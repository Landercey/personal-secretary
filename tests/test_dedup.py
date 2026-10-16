"""去重规则。

真踩过：去重键只取到日期，于是同一天的"上午十点集合"和"下午三点集合"
被当成同一件事，后一条**被静默丢掉** —— 用户看到的现象是
"我发的那条没反应"，而且没有任何提示。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import (                                        # noqa: E402
    SOURCE_MANUAL,
    SOURCE_QQ_GROUP,
    Message,
    TaskDraft,
)
from app.store.repository import SqliteStore, dedup_key_for, open_database  # noqa: E402


def draft(title: str, due: str | None = None, location: str | None = None) -> TaskDraft:
    return TaskDraft(title=title, due_at=due, location=location)


class TestDedupKey(unittest.TestCase):
    def test_same_title_same_time_same_place_is_one(self):
        a = draft("交作业", "2026-10-09 15:00:00", "图书馆")
        b = draft("交作业", "2026-10-09 15:00:00", "图书馆")
        self.assertEqual(dedup_key_for(a), dedup_key_for(b))

    def test_same_title_different_time_is_two(self):
        """同一天的两场活动是两件事 —— 这条是真的踩过。"""
        morning = draft("集合", "2026-10-09 10:00:00", "图书馆门口")
        afternoon = draft("集合", "2026-10-09 15:00:00", "图书馆门口")
        self.assertNotEqual(dedup_key_for(morning), dedup_key_for(afternoon))

    def test_same_title_different_place_is_two(self):
        here = draft("开会", "2026-10-09 15:00:00", "图书馆")
        there = draft("开会", "2026-10-09 15:00:00", "体育场")
        self.assertNotEqual(dedup_key_for(here), dedup_key_for(there))

    def test_whitespace_and_punctuation_tolerated(self):
        a = draft("交作业", "2026-10-09 15:00:00")
        b = draft(" 交 作业 ", "2026-10-09 15:00:00")
        self.assertEqual(dedup_key_for(a), dedup_key_for(b))

    def test_empty_title_means_no_key(self):
        """没有标题就不生成 key —— 否则一堆空标题会互相判重。"""
        self.assertEqual(dedup_key_for(draft("   ")), "")

    def test_missing_time_still_distinct_by_title(self):
        self.assertNotEqual(dedup_key_for(draft("买牛奶")), dedup_key_for(draft("拿快递")))


class TestDedupInStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SqliteStore(open_database(Path(self.tmp.name) / "d.db"))
        self.message = Message(msg_id="qq:1", source_kind=SOURCE_MANUAL, text="x")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_repeated_notice_keeps_one_task(self):
        """同一件事被转发两遍，只留一条。"""
        for _ in range(3):
            self.store.save_drafts(self.message, [
                draft("交作业", "2026-10-09 15:00:00", "图书馆")])
        self.assertEqual(self.store.count_tasks("open"), 1)

    def test_two_events_same_day_both_kept(self):
        """回归：同一天的两场活动都要留下，不能被当成重复丢掉。"""
        self.store.save_drafts(self.message, [
            draft("集合", "2026-10-09 10:00:00", "图书馆门口")])
        self.store.save_drafts(self.message, [
            draft("集合", "2026-10-09 15:00:00", "图书馆门口")])
        self.assertEqual(self.store.count_tasks("open"), 2)

    def test_duplicate_is_recorded_as_merged(self):
        """重复不是"什么都没发生"，要留一笔合并日志。"""
        self.store.save_drafts(self.message, [draft("交作业", "2026-10-09 15:00:00")])
        self.store.save_drafts(self.message, [draft("交作业", "2026-10-09 15:00:00")])
        row = self.store.conn.execute(
            "SELECT COUNT(*) n FROM task_events WHERE kind='merged'").fetchone()
        self.assertEqual(int(row["n"]), 1)


class TestCompletedNoticeRepeat(unittest.TestCase):
    """同一条通知，点过完成之后又发一遍 —— 不许再收一条、再推一次。

    2026-10-16 用户的原话："待办做完了他还推"。查库确认：营长把同一段话
    （"现要求四营的同学明天务必互相提醒穿着统一军训服参加军训！"）连着两天重发，
    解析出来的截止时间跟着往后挪一天 —— 去重键是"标题+时刻"，于是每天都是一条
    新待办、每天推一次（#37/#38 前一天点过完成，#40/#41 第二天又来了）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SqliteStore(open_database(Path(self.tmp.name) / "r.db"))
        self.message = Message(msg_id="qq:7", source_kind=SOURCE_QQ_GROUP,
                               source_name="某班群", text="x")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _only(self):
        return self.store.list_tasks("all")[0]

    def _notice(self, title, due):
        """群里来的草稿：source_kind 要写对，否则会被当成"手动录入"。"""
        return TaskDraft(title=title, due_at=due, source_kind=SOURCE_QQ_GROUP)

    def _merged_events(self) -> int:
        row = self.store.conn.execute(
            "SELECT COUNT(*) n FROM task_events WHERE kind='merged'").fetchone()
        return int(row["n"])

    def test_same_notice_after_completing_is_not_re_added(self):
        notice = "现要求四营的同学务必互相提醒穿着统一军训服参加军训"
        self.store.save_drafts(self.message, [self._notice(notice, "2026-10-16 23:59:00")])
        self.store.set_status(self._only().task_id, "done")
        # 第二天原样又发一遍：字一样，只是"明天"变成了新的一天
        self.store.save_drafts(self.message, [self._notice(notice, "2026-10-17 23:59:00")])
        self.assertEqual(self.store.count_tasks("open"), 0)
        self.assertEqual(self.store.count_tasks("done"), 1)
        self.assertEqual(self._merged_events(), 1)

    def test_a_notice_with_different_words_is_still_added(self):
        """措辞变了一点就是新通知，宁可多收一条。"""
        self.store.save_drafts(self.message, [
            self._notice("现要求四营的同学务必互相提醒穿着统一军训服参加军训",
                         "2026-10-16 23:59:00")])
        self.store.set_status(self._only().task_id, "done")
        self.store.save_drafts(self.message, [
            self._notice("现要求四营的同学务必互相提醒穿着统一军训服参加军训，另带腰带",
                         "2026-10-17 23:59:00")])
        self.assertEqual(self.store.count_tasks("open"), 1)

    def test_short_title_is_not_swallowed(self):
        """"拿快递"这种短标题又来一次，是真的又要做一遍。"""
        self.store.save_drafts(self.message, [self._notice("拿快递", "2026-10-16 20:00:00")])
        self.store.set_status(self._only().task_id, "done")
        self.store.save_drafts(self.message, [self._notice("拿快递", "2026-10-17 20:00:00")])
        self.assertEqual(self.store.count_tasks("open"), 1)

    def test_manual_re_entry_is_not_swallowed(self):
        """自己手敲的说明是特意又记一次，不能吞。"""
        manual = Message(msg_id="m:1", source_kind=SOURCE_MANUAL, text="x")
        title = "把志愿活动的报名表交到教务处"
        self.store.save_drafts(manual, [draft(title, "2026-10-16 20:00:00")])
        self.store.set_status(self._only().task_id, "done")
        self.store.save_drafts(manual, [draft(title, "2026-10-17 20:00:00")])
        self.assertEqual(self.store.count_tasks("open"), 1)


class TestDuplicateMerge(unittest.TestCase):
    """重复出现时**合并**进已有那条，而不是丢弃。

    为什么：重复出现说明这件事更要紧，丢掉等于用户第二次提到时程序装没看见。
    旧项目就是合并的，我第一版图省事直接丢，是个退步。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SqliteStore(open_database(Path(self.tmp.name) / "m.db"))
        self.message = Message(msg_id="qq:9", source_kind=SOURCE_MANUAL,
                               source_name="测试群", text="x")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_new_detail_is_kept_when_longer(self):
        self.store.save_drafts(self.message, [
            TaskDraft(title="交作业", due_at="2026-10-09 15:00:00", detail="短")])
        self.store.save_drafts(self.message, [
            TaskDraft(title="交作业", due_at="2026-10-09 15:00:00",
                      detail="这次带上了完整的作业要求，比原来长很多")])
        task = self.store.list_tasks("open")[0]
        self.assertIn("完整", task.detail)

    def test_existing_due_date_is_not_overwritten(self):
        """已经定过的时间不能被后一次转发的解析结果覆盖掉。

        同一条通知被转两遍，第二遍可能因为漏看一个字解析出别的时间。
        """
        self.store.save_drafts(self.message, [
            TaskDraft(title="交作业", due_at="2026-10-09 15:00:00")])
        self.store.save_drafts(self.message, [
            TaskDraft(title="交作业", due_at="2026-10-10 09:00:00")])
        self.assertEqual(self.store.list_tasks("open")[0].due_at, "2026-10-09 15:00:00")

    def test_missing_due_date_is_filled_in(self):
        """原来没时间的，后一次解析出来了就补上。"""
        self.store.save_drafts(self.message, [TaskDraft(title="交作业")])
        self.store.save_drafts(self.message, [
            TaskDraft(title="交作业", due_at="2026-10-09 15:00:00")])
        self.assertEqual(self.store.list_tasks("open")[0].due_at, "2026-10-09 15:00:00")

    def test_remind_level_takes_the_higher_one(self):
        self.store.save_drafts(self.message, [
            TaskDraft(title="交作业", due_at="2026-10-09 15:00:00", remind_level=1)])
        self.store.save_drafts(self.message, [
            TaskDraft(title="交作业", due_at="2026-10-09 15:00:00", remind_level=3)])
        self.assertEqual(self.store.list_tasks("open")[0].remind_level, 3)

    def test_still_only_one_task(self):
        for _ in range(5):
            self.store.save_drafts(self.message, [
                TaskDraft(title="交作业", due_at="2026-10-09 15:00:00")])
        self.assertEqual(self.store.count_tasks("open"), 1)


if __name__ == "__main__":
    unittest.main()
