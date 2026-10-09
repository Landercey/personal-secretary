"""存储层的分组查询。

加这个文件的直接原因：`overdue` 那条分支写错了一个逗号，
返回值少了一项，只有**真正调它**时才炸。旧的测试和旧的界面都没走到这条路，
所以它藏了很久，直到新界面第一次渲染左侧导航才现形。

每个分组都过一遍，就是防这种事。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import STATUS_DONE, SOURCE_MANUAL, Message, TaskDraft   # noqa: E402
from app.store.repository import SqliteStore, open_database                       # noqa: E402

ALL_VIEWS = ("open", "today", "tomorrow", "week", "overdue",
             "archive", "junk", "all")


def stamp(days: int, hour: int = 9) -> str:
    moment = datetime.now() + timedelta(days=days)
    return moment.replace(hour=hour, minute=0, second=0,
                          microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


class TestViews(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SqliteStore(open_database(Path(self.tmp.name) / "v.db"))
        message = Message(msg_id="m:1", source_kind=SOURCE_MANUAL,
                          source_name="测试", text="x")
        self.store.save_message(message)
        self.store.save_drafts(message, [
            TaskDraft(title="今天的事", due_at=stamp(0)),
            TaskDraft(title="明天的事", due_at=stamp(1)),
            TaskDraft(title="后天的事", due_at=stamp(2)),
            TaskDraft(title="上周欠的", due_at=stamp(-7)),
            TaskDraft(title="没写时间的"),
        ])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_every_view_is_queryable(self):
        """回归：每个分组都必须能查 —— overdue 少写一项就会在这里炸。"""
        for view in ALL_VIEWS:
            with self.subTest(view=view):
                rows = self.store.list_tasks(view)
                self.assertIsInstance(rows, list)
                self.assertIsInstance(self.store.count_tasks(view), int)

    def test_counts_match_listing_length(self):
        """计数和列表必须是同一个口径 —— 对不上界面上的数字就是骗人的。"""
        for view in ALL_VIEWS:
            with self.subTest(view=view):
                self.assertEqual(self.store.count_tasks(view),
                                 len(self.store.list_tasks(view, limit=500)))

    def test_today_and_tomorrow(self):
        titles = {t.title for t in self.store.list_tasks("today")}
        self.assertIn("今天的事", titles)
        self.assertNotIn("明天的事", titles)
        self.assertIn("明天的事", {t.title for t in self.store.list_tasks("tomorrow")})

    def test_overdue_only_past(self):
        """逾期 = 时间已经过了。

        注意别断言"逾期里只有上周那条" —— 今天 09:00 的事在中午看也逾期，
        那样写的话测试结果会随跑测试的钟点变。
        """
        titles = {t.title for t in self.store.list_tasks("overdue")}
        self.assertIn("上周欠的", titles)
        self.assertNotIn("明天的事", titles)
        self.assertNotIn("后天的事", titles)

    def test_week_includes_today_but_not_last_week(self):
        titles = {t.title for t in self.store.list_tasks("week")}
        self.assertIn("今天的事", titles)
        self.assertIn("后天的事", titles)
        self.assertNotIn("上周欠的", titles)

    def test_open_excludes_done(self):
        task = self.store.list_tasks("open")[0]
        self.store.set_status(task.task_id, STATUS_DONE, "测试")
        self.assertNotIn(task.task_id,
                         {t.task_id for t in self.store.list_tasks("open")})

    def test_unknown_view_falls_back_to_open(self):
        """写错分组名不该崩，退回"未完成"最安全。"""
        self.assertEqual(self.store.count_tasks("不存在的分组"),
                         self.store.count_tasks("open"))

    def test_all_view_sees_everything(self):
        self.assertEqual(self.store.count_tasks("all"),
                         self.store.count_tasks("open"))


if __name__ == "__main__":
    unittest.main()
