"""按"模拟一天日常使用"查出来的缺口写的一批测试。

这些不是凭空想的功能，是**真把一天的消息灌进管线**之后发现的：

  · 打勾完成的待办从所有列表里消失（没有"已完成"分组）
  · 点「这不是待办」的那条哪儿都找不回来（两套废件箱在打架）
  · 解析错了没法改，只能"判误判再重新发一遍"
  · 记住了原话却搜不到
  · 界面上看不出收消息的进程到底还活着没有

每一条都按"用户会怎么碰到它"来测。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import (                            # noqa: E402
    STATUS_DONE, STATUS_DROPPED, STATUS_OPEN, SOURCE_MANUAL, Attachment, Message,
    TaskDraft,
)
from app.runtime import heartbeat                              # noqa: E402
from app.store.repository import SqliteStore, open_database    # noqa: E402
from app.ui.bridge import Api, _normalize_due                  # noqa: E402


def stamp(days: int, hour: int = 9) -> str:
    """今天往后第 N 天的某个整点。"""
    moment = datetime.now() + timedelta(days=days)
    return moment.replace(hour=hour, minute=0, second=0,
                          microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SqliteStore(open_database(Path(self.tmp.name) / "t.db"))
        self.api = Api(self.store, actions={"add_text": lambda text: 1})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, title: str, due: str | None = None, location: str | None = None,
            *, quoted: str = "", detail: str = "") -> int:
        message = Message(msg_id=f"t:{title}", source_kind=SOURCE_MANUAL,
                          source_name="手动录入", text=title)
        drafts = [TaskDraft(title=title, due_at=due, location=location,
                            quoted=quoted or title, detail=detail,
                            origin_msg_id=message.msg_id)]
        created = self.store.save_drafts(message, drafts)
        return created[0].task_id


class TestNoDueDateCountsAsToday(Base):
    """没写时间的待办，算「今天」。

    用户定的："要不默认今天"。不这么算的话，`拿快递` 这种随手记的
    只有点开「全部未完成」才看得到 —— 默认视图是「今天」，他会以为它丢了。

    但**只在「今天」**这么算：「明天/本周」问的是"那一天有什么事"，
    无时间的塞进去才叫乱；「逾期」更不能算 —— 它没时间，凭什么算过期。
    """

    def test_undated_shows_up_in_today(self):
        task_id = self.add("拿快递")
        self.assertIn(task_id, [t.task_id for t in self.store.list_tasks("today")])
        self.assertEqual(self.store.count_tasks("today"), 1)

    def test_undated_stays_in_the_all_list(self):
        task_id = self.add("拿快递")
        self.assertIn(task_id, [t.task_id for t in self.store.list_tasks("open")])

    def test_undated_is_not_tomorrow_or_this_week(self):
        self.add("拿快递")
        for view in ("tomorrow", "week", "next_week"):
            with self.subTest(view=view):
                self.assertEqual(self.store.list_tasks(view), [])

    def test_undated_is_never_overdue(self):
        """没写时间的东西凭什么算过期？变红了用户只会莫名其妙。"""
        self.add("拿快递")
        self.assertEqual(self.store.list_tasks("overdue"), [])

    def test_it_shows_up_together_with_timed_ones(self):
        timed = self.add("交作业", due=stamp(0, 15))
        undated = self.add("拿快递")
        today = [t.task_id for t in self.store.list_tasks("today")]

        self.assertEqual(set(today), {timed, undated})
        # 有时间的排前面，没时间的垫底（先看赶时间的）
        self.assertEqual(today[0], timed)

    def test_edit_clearing_the_time_puts_it_back_into_today(self):
        """「改内容」把时间清空之后，它应该还在今天，不是消失。"""
        task_id = self.add("交作业", due=stamp(5))
        self.assertEqual(self.store.list_tasks("today"), [])

        self.api.edit_task(task_id, "交作业", "", "")

        self.assertIn(task_id, [t.task_id for t in self.store.list_tasks("today")])


class TestDoneView(Base):
    """打勾之后那条得还看得见。"""

    def test_completed_task_shows_up_under_done(self):
        task_id = self.add("拿快递")
        self.store.set_status(task_id, STATUS_DONE, "界面里标记完成")

        self.assertEqual([t.task_id for t in self.store.list_tasks("done")], [task_id])
        self.assertEqual(self.store.count_tasks("done"), 1)
        self.assertEqual(self.store.count_tasks("open"), 0)

    def test_done_and_open_do_not_mix(self):
        keep = self.add("交作业")
        finished = self.add("拿快递")
        self.store.set_status(finished, STATUS_DONE, "x")

        self.assertEqual([t.task_id for t in self.store.list_tasks("open")], [keep])
        self.assertEqual([t.task_id for t in self.store.list_tasks("done")], [finished])

    def test_can_undo_completion(self):
        """手滑点完成，得能放回去。"""
        task_id = self.add("拿快递")
        self.store.set_status(task_id, STATUS_DONE, "x")
        self.api.restore(task_id)

        self.assertEqual([t.task_id for t in self.store.list_tasks("open")], [task_id])
        self.assertEqual(self.store.count_tasks("done"), 0)


class TestJunkBoxMerges(Base):
    """废件箱得把两样东西合在一起，而且点错的能恢复。"""

    def test_both_kinds_show_up(self):
        task_id = self.add("这不是待办的一条")
        self.store.set_status(task_id, STATUS_DROPPED, "界面里判定为误判")
        self.store.log_dropped_text("哈哈哈哈", "闲聊内容", "qq_group", "某群", "m:1")

        items = self.api.dropped()["items"]
        kinds = sorted(item["kind"] for item in items)
        self.assertEqual(kinds, ["task", "text"])

    def test_dropped_task_can_come_back(self):
        task_id = self.add("其实是要做的")
        self.store.set_status(task_id, STATUS_DROPPED, "手滑")

        before = {item["kind"] for item in self.api.dropped()["items"]}
        self.assertIn("task", before)

        self.api.restore(task_id)
        self.assertEqual([t.task_id for t in self.store.list_tasks("open")], [task_id])
        self.assertNotIn("task", {i["kind"] for i in self.api.dropped()["items"]})

    def test_restore_keeps_the_drop_reason_for_future_tuning(self):
        """恢复不等于抹掉记录 —— 那条"我判错过"是以后调规则的案例。"""
        task_id = self.add("x")
        self.store.set_status(task_id, STATUS_DROPPED, "当时觉得是误判")
        self.api.restore(task_id)

        task = self.store.get_task(task_id)
        self.assertEqual(task.status, STATUS_OPEN)
        self.assertEqual(task.drop_reason, "当时觉得是误判")


class TestMoveOut(Base):
    """"移出"= 回到「全部未完成」。目标只有一个，落在哪个分组由时间自己决定。

    用户报的原话："已完成的，归档的没有移出的按钮，还有要想好移能移到哪"。
    查下来是两层问题：
      · 界面上归档的确实没按钮；
      · **代码里也没有反向操作** —— `archive()` 有，`unarchive()` 没有，
        所以归档之后根本移不出来。`restore()` 只改 status，不碰 is_archived，
        对归档的等于什么都没做。
    """

    def test_archived_can_come_back(self):
        task_id = self.add("去年的团费", due=stamp(1))
        self.store.archive(task_id, "过期了")
        self.assertEqual(self.store.list_tasks("open"), [])

        self.api.restore(task_id)

        self.assertEqual([t.task_id for t in self.store.list_tasks("open")], [task_id])
        self.assertEqual(self.store.count_tasks("archive"), 0)

    def test_restore_must_clear_the_archive_flag(self):
        """归档的 status 本来就是 open —— 光改 status 是看不见效果的。

        这就是原来那个 bug：点了恢复，界面上什么都没变。
        """
        task_id = self.add("x")
        self.store.archive(task_id, "x")
        self.assertEqual(self.store.get_task(task_id).status, STATUS_OPEN)
        self.assertTrue(self.store.get_task(task_id).is_archived)

        self.api.restore(task_id)

        self.assertFalse(self.store.get_task(task_id).is_archived)

    def test_moved_out_task_lands_where_its_time_says(self):
        """移出之后不用选去哪：它自己按截止时间回到该去的分组。"""
        today = self.add("今天的事", due=stamp(0))
        far = self.add("下个月的事", due=stamp(40))
        for task_id in (today, far):
            self.store.archive(task_id, "x")
            self.api.restore(task_id)

        opens = {t.task_id for t in self.store.list_tasks("open")}
        self.assertEqual(opens, {today, far})
        self.assertIn(today, [t.task_id for t in self.store.list_tasks("today")])
        self.assertNotIn(far, [t.task_id for t in self.store.list_tasks("today")])

    def test_move_out_from_done(self):
        task_id = self.add("x")
        self.store.set_status(task_id, STATUS_DONE, "x")
        self.api.restore(task_id)

        self.assertEqual(self.store.count_tasks("done"), 0)
        self.assertEqual(self.store.count_tasks("open"), 1)

    def test_move_out_from_junk(self):
        task_id = self.add("我点错了")
        self.store.set_status(task_id, STATUS_DROPPED, "手滑")
        self.api.restore(task_id)

        self.assertEqual(self.store.count_tasks("junk"), 0)
        self.assertEqual(self.store.count_tasks("open"), 1)

    def test_ui_can_tell_an_archived_task_apart(self):
        """界面得知道这条是不是归档的，不然不知道该显示哪个"移出"按钮。"""
        task_id = self.add("x")
        self.store.archive(task_id, "x")

        item = self.api.tasks("archive")["items"][0]
        self.assertTrue(item["archived"])
        self.assertFalse(item["done"])


class TestEdit(Base):
    """解析错了得能改，而不是"判误判再重新发一遍"。"""

    def test_edit_title_time_location(self):
        task_id = self.add("交作业", due="2026-10-10 09:00:00", location="实验楼302")
        result = self.api.edit_task(task_id, " 交实验报告 ", "2026-10-12T14:30", " 6A-305 ")

        self.assertEqual(result, {"ok": True})
        task = self.store.get_task(task_id)
        self.assertEqual(task.title, "交实验报告")
        self.assertEqual(task.due_at, "2026-10-12 14:30:00")
        self.assertEqual(task.location, "6A-305")

    def test_empty_time_clears_it(self):
        task_id = self.add("交作业", due="2026-10-10 09:00:00")
        self.api.edit_task(task_id, "交作业", "", "")
        self.assertIsNone(self.store.get_task(task_id).due_at)

    def test_empty_title_is_refused(self):
        task_id = self.add("交作业")
        self.assertFalse(self.api.edit_task(task_id, "   ", "", "")["ok"])
        self.assertEqual(self.store.get_task(task_id).title, "交作业")

    def test_dedup_key_follows_the_edit(self):
        """改完标题之后，同一条消息再来一次不能又建一条新的。"""
        task_id = self.add("交作业", due="2026-10-10 09:00:00")
        self.api.edit_task(task_id, "交实验报告", "2026-10-12T14:30", "6A-305")
        before = self.store.get_task(task_id).dedup_key

        # 照着改完之后的写法再发一遍
        message = Message(msg_id="t:again", source_kind=SOURCE_MANUAL,
                          source_name="手动录入", text="交实验报告")
        created = self.store.save_drafts(message, [TaskDraft(
            title="交实验报告", due_at="2026-10-12 14:30:00",
            location="6A-305", origin_msg_id="t:again")])

        self.assertEqual(created, [])                       # 没新建
        self.assertEqual(self.store.get_task(task_id).dedup_key, before)
        self.assertEqual(self.store.count_tasks("open"), 1)

    def test_time_shapes(self):
        self.assertEqual(_normalize_due("2026-10-12T09:00"), "2026-10-12 09:00:00")
        self.assertEqual(_normalize_due("2026-10-12"), "2026-10-12 23:59:00")
        self.assertEqual(_normalize_due(""), "")


class TestSearch(Base):
    """用户记得的往往是原话，不是我们提炼的标题。"""

    def test_finds_by_title(self):
        self.add("交实验报告")
        self.assertEqual([t.title for t in self.store.search("实验")], ["交实验报告"])

    def test_finds_by_original_quote(self):
        self.add("集合", quoted="明天下午三点在图书馆门口集合")
        self.assertEqual([t.title for t in self.store.search("图书馆")], ["集合"])

    def test_finds_archived_too(self):
        task_id = self.add("去年的团费")
        self.store.archive(task_id, "过期了")
        self.assertEqual(len(self.store.search("团费")), 1)

    def test_empty_keyword_returns_nothing(self):
        self.add("交作业")
        self.assertEqual(self.store.search(""), [])
        self.assertEqual(self.store.search("   "), [])

    def test_bridge_returns_editable_shape(self):
        self.add("交作业", due="2026-10-12 09:00:00")
        item = self.api.search("作业")["items"][0]
        self.assertEqual(item["due_raw"], "2026-10-12T09:00")
        self.assertIn("files", item)

    def test_search_stays_inside_the_current_view(self):
        """用户报的：在「今天」里搜索，已完成/已归档/下个月的**全冒出来了**。

        "不搜索没事，一搜全出来了" —— 搜索框就在那个分组的标题旁边，
        预期当然是"在这个列表里筛"。
        """
        today = self.add("今天交作业", due=stamp(0))
        self.add("下个月的事", due=stamp(40))
        archived = self.add("去年的作业总结")
        self.store.archive(archived, "过期")
        finished = self.add("已经做完的作业")
        self.store.set_status(finished, STATUS_DONE, "x")

        found = [t.task_id for t in self.store.search("作业", view="today")]
        self.assertEqual(found, [today])

        # 全库搜（view="all"）才把归档和已完成的也捞出来
        everything = {t.task_id for t in self.store.search("作业", view="all")}
        self.assertIn(archived, everything)
        self.assertIn(finished, everything)

    def test_bridge_passes_the_view_through(self):
        self.add("今天交作业", due=stamp(0))
        self.add("下个月的作业", due=stamp(40))
        result = self.api.search("作业", "today")
        self.assertEqual(result["view"], "today")
        self.assertEqual(len(result["items"]), 1)


class TestReceiverHeartbeat(Base):
    """界面得看得出"收消息的进程还活着没有"。"""

    def test_never_beaten_reads_as_dead(self):
        self.assertFalse(self.api.config_summary()["receiver"]["alive"])

    def test_fresh_beat_reads_as_alive(self):
        heartbeat.beat(self.store, "登录号 123｜收到 5", force=True)
        state = self.api.config_summary()["receiver"]

        self.assertTrue(state["alive"])
        self.assertIn("登录号 123", state["detail"])

    def test_stale_beat_reads_as_dead(self):
        """进程被杀掉之后，心跳会自己变旧 —— 不用等它说再见。"""
        heartbeat.beat(self.store, "旧的", force=True)
        self.store.set_meta(heartbeat.KEY, '{"at": "2020-01-01 00:00:00", "detail": "旧的"}')
        state = self.api.config_summary()["receiver"]

        self.assertFalse(state["alive"])
        self.assertGreater(state["age_seconds"], 60)

    def test_broken_heartbeat_does_not_crash_anything(self):
        self.store.set_meta(heartbeat.KEY, "这不是 json")
        self.assertFalse(self.api.config_summary()["receiver"]["alive"])

    def test_beat_throttles_itself(self):
        """调用方可以无脑每轮喊一次，写盘要自己节流。"""
        heartbeat.beat(self.store, "第一次", force=True)
        first = self.store.get_meta(heartbeat.KEY)
        heartbeat._last_write = time.monotonic()          # 刚写过
        heartbeat.beat(self.store, "第二次")
        self.assertEqual(self.store.get_meta(heartbeat.KEY), first)


class TestAttachmentDedup(Base):
    """同一个文件挂到同一条待办上只留一份。"""

    def test_same_file_twice_keeps_one_row(self):
        blob = self.tmp_path("a.txt").write_bytes(b"hello")
        attachment = Attachment(name="a.txt", path=str(self.tmp_path("a.txt")),
                                mime="text/plain", size=5, kind="other",
                                sha256="deadbeef")
        message = Message(msg_id="t:1", source_kind=SOURCE_MANUAL, source_name="手动录入")
        drafts = [TaskDraft(title="交作业", origin_msg_id="t:1",
                            attachments=[attachment])]
        task_id = self.store.save_drafts(message, drafts)[0].task_id

        # 同一条再来一遍（走合并那条路）
        self.store.save_drafts(message, [TaskDraft(title="交作业", origin_msg_id="t:2",
                                                   attachments=[attachment])])
        self.assertEqual(len(self.store.list_attachments(task_id)), 1)
        self.assertIsNotNone(blob)

    def tmp_path(self, name: str) -> Path:
        return Path(self.tmp.name) / name


class TestAttachmentAccess(Base):
    """界面只能碰库里记着的附件 —— 不然就是个"打开任意文件"的后门。"""

    def test_unknown_path_is_refused(self):
        task_id = self.add("交作业")
        with self.assertRaises(ValueError):
            self.api._known_attachment(task_id, "C:/Windows/System32/cmd.exe")
        with self.assertRaises(ValueError):
            self.api.open_attachment(task_id, "C:/Windows/notepad.exe")

    def test_known_path_is_returned(self):
        target = Path(self.tmp.name) / "图.png"
        target.write_bytes(b"\x89PNG")
        attachment = Attachment(name="图.png", path=str(target), mime="image/png",
                                size=4, kind="image", sha256="x")
        message = Message(msg_id="t:1", source_kind=SOURCE_MANUAL, source_name="手动录入")
        task_id = self.store.save_drafts(message, [TaskDraft(
            title="看图", origin_msg_id="t:1", attachments=[attachment])])[0].task_id

        self.assertEqual(self.api._known_attachment(task_id, str(target)), target)

    def test_missing_file_says_so(self):
        target = Path(self.tmp.name) / "没了.png"
        attachment = Attachment(name="没了.png", path=str(target), mime="image/png",
                                size=1, kind="image", sha256="y")
        message = Message(msg_id="t:2", source_kind=SOURCE_MANUAL, source_name="手动录入")
        task_id = self.store.save_drafts(message, [TaskDraft(
            title="看图", origin_msg_id="t:2", attachments=[attachment])])[0].task_id

        with self.assertRaises(ValueError) as caught:
            self.api._known_attachment(task_id, str(target))
        self.assertIn("不在了", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
