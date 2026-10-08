"""第 7 步门禁：定时调度。

全部离线、不真发消息、不真等到早上七点半 —— `due_slots()` 是纯计算，
`run_once(now=...)` 可以喂任意时刻进来。

这里盯住的是"用户会觉得程序有毛病"的那几件事：
  · 同一份早报推了两遍（重启一次就重发一遍）
  · 明明晚上十点才开机，还补推"今天的早报"
  · 打游戏时被推送弹到桌面
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.interfaces import Sink, SinkResult                  # noqa: E402
from app.contracts.models import SOURCE_MANUAL, Message, TaskDraft     # noqa: E402
from app.runtime import scheduler as sch                               # noqa: E402
from app.runtime.config import config                                  # noqa: E402
from app.store.repository import SqliteStore, open_database            # noqa: E402


class FakeSink(Sink):
    """假通道：只记下发了什么，不联网。"""

    name = "fake"

    def __init__(self, ok: bool = True, boom: bool = False) -> None:
        self.ok = ok
        self.boom = boom
        self.sent: list = []

    def deliver(self, payload):
        if self.boom:
            raise RuntimeError("通道炸了")
        self.sent.append(payload)
        return SinkResult(ok=self.ok, code=0 if self.ok else -1,
                          message="ok" if self.ok else "假装失败")


def at(text: str) -> datetime:
    """把 "2026-10-08 07:30" 变成 datetime，省得测试里写一堆参数。"""
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


class TestParseTimes(unittest.TestCase):
    def test_normalises_and_sorts(self):
        self.assertEqual(sch.parse_times(["21:30", "7:30"]), ["07:30", "21:30"])

    def test_accepts_chinese_colon_and_comma(self):
        self.assertEqual(sch.parse_times("7：30，21:30"), ["07:30", "21:30"])

    def test_drops_garbage_without_crashing(self):
        """配置写坏了不能让调度器整个不工作。"""
        self.assertEqual(sch.parse_times(["", "abc", "25:00", "07:30", "12:99"]),
                         ["07:30"])

    def test_empty(self):
        self.assertEqual(sch.parse_times(None), [])
        self.assertEqual(sch.parse_times([]), [])


class TestDueSlots(unittest.TestCase):
    TIMES = ["07:30", "21:30"]

    def test_before_slot_nothing_is_due(self):
        self.assertEqual(sch.due_slots(at("2026-10-08 07:00"), times=self.TIMES), [])

    def test_right_after_slot_is_due(self):
        slots = sch.due_slots(at("2026-10-08 07:31"), times=self.TIMES)
        self.assertEqual([s.label for s in slots], ["07:30"])
        self.assertEqual(slots[0].batch, "2026-10-08 07:30")

    def test_within_catch_up_window_still_due(self):
        """早上 7:30 关机、9:00 才开 —— 这份早报还值得推（差 90 分钟）。"""
        slots = sch.due_slots(at("2026-10-08 09:00"), times=self.TIMES,
                              catch_up_minutes=120)
        self.assertEqual([s.label for s in slots], ["07:30"])

    def test_beyond_catch_up_window_not_due(self):
        """晚上 10 点才开机，再推"今天的早报"就是纯打扰。"""
        slots = sch.due_slots(at("2026-10-08 22:00"), times=self.TIMES,
                              catch_up_minutes=120)
        self.assertEqual([s.label for s in slots], ["21:30"])

    def test_zero_catch_up_means_never(self):
        """填 0 = 只在那一分钟里推，过了就不补。

        30 秒一轮的检查足够在那一分钟里命中，所以这不会漏推。
        """
        slots = sch.due_slots(at("2026-10-08 07:30"), times=self.TIMES,
                              catch_up_minutes=0)
        self.assertEqual([s.label for s in slots], ["07:30"])
        slots = sch.due_slots(at("2026-10-08 07:31"), times=self.TIMES,
                              catch_up_minutes=0)
        self.assertEqual(slots, [])

    def test_both_slots_due_late_in_day(self):
        """补推窗口够长时，一天两个点都可能该推。"""
        slots = sch.due_slots(at("2026-10-08 23:00"), times=self.TIMES,
                              catch_up_minutes=1440)
        self.assertEqual([s.label for s in slots], ["07:30", "21:30"])


class TestSlotTitle(unittest.TestCase):
    def test_morning(self):
        self.assertEqual(sch.slot_title(at("2026-10-08 07:30")), "今天要做的")

    def test_evening(self):
        self.assertEqual(sch.slot_title(at("2026-10-08 21:30")), "明天要留意的")

    def test_midday(self):
        self.assertEqual(sch.slot_title(at("2026-10-08 13:00")), "待办汇总")


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SqliteStore(open_database(Path(self.tmp.name) / "s.db"))
        message = Message(msg_id="m:1", source_kind=SOURCE_MANUAL, text="x")
        self.store.save_message(message)
        self.store.save_drafts(message, [
            TaskDraft(title="交作业", due_at="2026-10-09 15:00:00"),
            TaskDraft(title="拿快递"),
        ])
        self.sink = FakeSink()
        self.scheduler = sch.Scheduler(self.store, self.sink, tick=0.01)
        self._saved = {
            "times": list(config.get("push", "times", []) or []),
            "enabled": config.get("push", "enabled", True),
        }
        config["push"]["times"] = ["07:30", "21:30"]
        config["push"]["enabled"] = True

    def tearDown(self):
        config["push"]["times"] = self._saved["times"]
        config["push"]["enabled"] = self._saved["enabled"]
        self.store.close()
        self.tmp.cleanup()

    def test_delivers_once_at_slot(self):
        sent = self.scheduler.run_once(at("2026-10-08 07:31"))
        self.assertEqual(sent, ["2026-10-08 07:30"])
        self.assertEqual(len(self.sink.sent), 1)
        self.assertIn("交作业", self.sink.sent[0].body)

    def test_does_not_deliver_twice(self):
        """同一份早报只推一次 —— 重启一次就重发一遍是最招人烦的。"""
        self.scheduler.run_once(at("2026-10-08 07:31"))
        self.scheduler.run_once(at("2026-10-08 07:35"))
        self.scheduler.run_once(at("2026-10-08 07:59"))
        self.assertEqual(len(self.sink.sent), 1)

    def test_second_slot_delivers_too(self):
        self.scheduler.run_once(at("2026-10-08 07:31"))
        self.scheduler.run_once(at("2026-10-08 21:31"))
        self.assertEqual(len(self.sink.sent), 2)

    def test_different_days_are_different_batches(self):
        self.scheduler.run_once(at("2026-10-08 07:31"))
        self.scheduler.run_once(at("2026-10-09 07:31"))
        self.assertEqual(len(self.sink.sent), 2)

    def test_disabled_scheduler_sends_nothing(self):
        config["push"]["enabled"] = False
        self.assertEqual(self.scheduler.run_once(at("2026-10-08 07:31")), [])
        self.assertEqual(self.sink.sent, [])

    def test_no_slots_configured(self):
        config["push"]["times"] = []
        self.assertEqual(self.scheduler.run_once(at("2026-10-08 07:31")), [])

    def test_failed_push_is_recorded_and_retried_next_tick(self):
        """推失败要记账（且**不**算推过），下一次检查还能重试。"""
        self.sink.ok = False
        self.assertEqual(self.scheduler.run_once(at("2026-10-08 07:31")), [])
        records = self.store.recent_pushes()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["ok"], 0)
        # 下一轮换一个好通道，应该补上
        self.scheduler.sink = FakeSink(ok=True)
        self.assertEqual(self.scheduler.run_once(at("2026-10-08 07:35")),
                         ["2026-10-08 07:30"])

    def test_sink_exception_does_not_crash(self):
        """通道抛异常不能让调度器停摆。"""
        self.scheduler.sink = FakeSink(boom=True)
        self.assertEqual(self.scheduler.run_once(at("2026-10-08 07:31")), [])
        self.assertEqual(self.store.recent_pushes()[0]["ok"], 0)

    def test_digest_carries_attachments(self):
        from app.contracts.models import KIND_IMAGE, Attachment

        path = Path(self.tmp.name) / "pic.png"
        path.write_bytes(b"x")
        task = self.store.list_tasks("open")[0]
        with self.store.conn:
            self.store.conn.execute(
                "INSERT INTO attachments (task_id,msg_id,name,path,mime,size,kind,sha256)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (task.task_id, "", "pic.png", str(path), "image/png", 1,
                 KIND_IMAGE, ""))
        self.scheduler.run_once(at("2026-10-08 07:31"))
        self.assertTrue(self.sink.sent[0].attachments)

    def test_memory_gate_blocks_pushing(self):
        """内存吃紧时一条都不推 —— 打游戏时被弹到桌面最招人烦。"""
        original = config.get("resource", "mem_critical_percent", 88)
        try:
            config["resource"]["mem_critical_percent"] = 0   # 必然触发
            self.assertEqual(self.scheduler.run_once(at("2026-10-08 07:31")), [])
            self.assertEqual(self.sink.sent, [])
        finally:
            config["resource"]["mem_critical_percent"] = original

    def test_describe_is_readable(self):
        text = self.scheduler.describe()
        self.assertIn("07:30", text)
        self.assertIn("21:30", text)


class TestPushRecords(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SqliteStore(open_database(Path(self.tmp.name) / "p.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_only_successful_pushes_count_as_sent(self):
        self.store.log_push("2026-10-08 07:30", "fake", False, "失败了")
        self.assertFalse(self.store.already_pushed("2026-10-08 07:30"))
        self.store.log_push("2026-10-08 07:30", "fake", True, "ok")
        self.assertTrue(self.store.already_pushed("2026-10-08 07:30"))

    def test_recent_pushes_newest_first(self):
        self.store.log_push("a", "fake", True, "1")
        self.store.log_push("b", "fake", True, "2")
        self.assertEqual(self.store.recent_pushes()[0]["batch"], "b")


if __name__ == "__main__":
    unittest.main()
