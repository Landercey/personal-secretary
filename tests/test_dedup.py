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

from app.contracts.models import SOURCE_MANUAL, Message, TaskDraft        # noqa: E402
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

    def test_duplicate_is_recorded_in_events(self):
        """被去重挡掉不是"什么都没发生"，要留一笔日志。"""
        self.store.save_drafts(self.message, [draft("交作业", "2026-10-09 15:00:00")])
        self.store.save_drafts(self.message, [draft("交作业", "2026-10-09 15:00:00")])
        row = self.store.conn.execute(
            "SELECT COUNT(*) n FROM task_events WHERE kind='duplicate'").fetchone()
        self.assertEqual(int(row["n"]), 1)


if __name__ == "__main__":
    unittest.main()
