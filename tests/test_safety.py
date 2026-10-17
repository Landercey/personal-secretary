"""几道"别出事"的闸：备份、自动重连额度、免打扰、批量归档。

这四样都是"平时看不见、出事时救命"的东西：
  · 备份 —— 用户丢过一次配置、一次待办；
  · 重连额度 —— 反复带号登录才是真的会被风控；
  · 免打扰 —— 半夜两点的"还有 3 分钟"对谁都没用；
  · 批量归档 —— 40 条已完成一条条点太慢。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import SOURCE_QQ_GROUP, Message, TaskDraft   # noqa: E402
from app.store.repository import SqliteStore, open_database           # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._old_home = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = str(self.home)
        (self.home / "data").mkdir()
        self.store = SqliteStore(open_database(self.home / "data" / "secretary.db"))
        self.message = Message(msg_id="qq:1", source_kind=SOURCE_QQ_GROUP,
                               source_name="测试群", text="明天下午三点开会")

    def tearDown(self):
        self.store.close()
        if self._old_home is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old_home
        self.tmp.cleanup()

    def add(self, title: str, due: str | None = None):
        return self.store.save_drafts(
            self.message, [TaskDraft(title=title, due_at=due)])[0]


class TestBackup(Base):
    def test_one_backup_per_day_and_the_config_rides_along(self):
        from app.runtime import backup

        (self.home / "config.toml").write_text("ok = 1\n", encoding="utf-8")
        first = backup.run_daily()
        self.assertIsNotNone(first)
        self.assertTrue(first.is_file())
        self.assertGreater(first.stat().st_size, 0)
        self.assertTrue((backup.folder() / f"config-{first.stem[-8:]}.toml").is_file())
        # 同一天再跑一次：不重复备
        self.assertIsNone(backup.run_daily())

    def test_it_keeps_only_the_newest_seven(self):
        from app.runtime import backup

        folder = backup.folder()
        folder.mkdir(parents=True, exist_ok=True)
        for day in range(1, 11):
            (folder / f"secretary-202610{day:02d}.db").write_bytes(b"x")
        backup.run_daily()
        left = sorted(folder.glob("secretary-*.db"))
        self.assertEqual(len(left), backup.KEEP)
        self.assertNotIn("20261001", left[0].name)


class TestRestartBudget(Base):
    def test_the_daily_cap_holds_and_is_remembered(self):
        from app.runtime import restart_budget

        self.assertTrue(restart_budget.allow(self.store, 2))
        restart_budget.note(self.store)
        restart_budget.note(self.store)
        self.assertEqual(restart_budget.used(self.store), 2)
        self.assertFalse(restart_budget.allow(self.store, 2),
                         "额度用完还放行，等于一天能登十几次")

    def test_zero_means_unlimited(self):
        from app.runtime import restart_budget

        for _ in range(20):
            restart_budget.note(self.store)
        self.assertTrue(restart_budget.allow(self.store, 0))


class TestQuietHours(unittest.TestCase):
    def test_the_default_window_wraps_past_midnight(self):
        from app.runtime.scheduler import in_quiet_hours

        window = "23:00-07:30"
        self.assertTrue(in_quiet_hours(datetime(2026, 10, 17, 23, 30), window))
        self.assertTrue(in_quiet_hours(datetime(2026, 10, 17, 3, 0), window))
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 17, 8, 0), window))
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 17, 22, 59), window))

    def test_empty_or_broken_means_off(self):
        from app.runtime.scheduler import in_quiet_hours

        for window in ("", "  ", "abc", "25:00-07:00", "08:00-08:00"):
            with self.subTest(window=window):
                self.assertFalse(
                    in_quiet_hours(datetime(2026, 10, 17, 23, 30), window))

    def test_a_reminder_inside_quiet_hours_is_skipped(self):
        """免打扰只压临期提醒，汇总照发（汇总时间是用户自己挑的）。"""
        from app.runtime import scheduler as sch

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SqliteStore(open_database(Path(tmp.name) / "q.db"))
        self.addCleanup(store.close)
        message = Message(msg_id="m:1", source_kind=SOURCE_QQ_GROUP, text="x")
        # 群消息默认带 "60,30,10,3" 四档；10 分钟那一档在 03:00 正好进窗口
        store.save_drafts(message, [TaskDraft(
            title="开会", due_at="2026-10-17 03:10:00")])

        sent = []

        class Sink:
            name = "console"

            def deliver(self, payload):
                sent.append(payload)
                from app.contracts.interfaces import SinkResult
                return SinkResult(ok=True, message="ok")

        job = sch.Scheduler(store, Sink())
        job._deliver_due_reminders(datetime(2026, 10, 17, 3, 0))
        self.assertEqual(sent, [], "半夜三点不该单独提醒")


class TestSearchAndBulk(Base):
    def test_search_finds_the_original_message_not_only_tasks(self):
        """用户记得的是原话（"班长说的截止几号"），不只是我们提炼的标题。"""
        self.store.save_message(Message(
            msg_id="qq:9", source_kind=SOURCE_QQ_GROUP, source_name="某班群",
            sender="班长", sent_at="2026-10-10 12:00:00",
            text="论文截止到 11 月 3 号晚上 12 点，交到学习通"))

        hits = self.store.search_messages("11 月 3 号")
        self.assertEqual(len(hits), 1)
        self.assertIn("论文", hits[0]["text"])
        self.assertEqual(hits[0]["source"], "某班群")
        self.assertEqual(self.store.search_messages("  "), [])

    def test_archive_all_done_moves_them_out_of_the_way(self):
        a = self.add("一")
        b = self.add("二")
        self.add("三")
        self.store.set_status(a.task_id, "done")
        self.store.set_status(b.task_id, "done")

        self.assertEqual(self.store.archive_all_done(), 2)
        self.assertEqual(self.store.count_tasks("done"), 0)
        self.assertEqual(self.store.count_tasks("archive"), 2)
