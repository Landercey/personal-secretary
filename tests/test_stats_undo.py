"""战报 + 撤销：让用户看见过滤在干活，也让手滑能退回去。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import SOURCE_QQ_GROUP, Message, TaskDraft   # noqa: E402
from app.store.repository import SqliteStore, open_database           # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._old = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = str(self.home)
        (self.home / "data").mkdir()
        self.store = SqliteStore(open_database(self.home / "data" / "secretary.db"))
        self.message = Message(msg_id="qq:1", source_kind=SOURCE_QQ_GROUP,
                               source_name="某班群", sender="班长", text="x")

    def tearDown(self):
        self.store.close()
        if self._old is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old
        self.tmp.cleanup()

    def add(self, title: str):
        return self.store.save_drafts(
            self.message, [TaskDraft(title=title)])[0]

    def api(self):
        from app.ui.bridge import Api

        return Api(self.store, [], {}, notices=[])


class TestTodayStats(Base):
    def test_it_counts_messages_tasks_and_junk(self):
        self.store.save_message(self.message)
        self.add("交作业")
        self.store.log_dropped_text("哈哈哈", "闲聊", "qq_group", "某班群", "qq:1")

        stats = self.api().today_stats()
        self.assertEqual(stats["received"], 1)
        self.assertEqual(stats["kept"], 1)
        self.assertEqual(stats["junk"], 1)

    def test_an_empty_day_is_all_zero(self):
        stats = self.store.today_stats()
        self.assertEqual(set(stats.values()), {0})


class TestUndo(Base):
    def test_two_tasks_at_the_same_minute_are_flagged_as_a_clash(self):
        """同一分钟排了两件事 —— 列表上要提醒一句，不然只能自己发现撞车。"""
        self.store.save_drafts(self.message, [TaskDraft(title="开会", due_at="2026-10-18 13:30")])
        self.store.save_drafts(self.message, [TaskDraft(title="交表", due_at="2026-10-18 13:30")])
        self.store.save_drafts(self.message, [TaskDraft(title="跑步", due_at="2026-10-18 15:00")])
        flags = {i["title"]: i["conflict"] for i in self.api().tasks("open")["items"]}
        self.assertEqual(flags, {"开会": True, "交表": True, "跑步": False})

    def test_completing_one_can_be_undone(self):
        task = self.add("交作业")
        api = self.api()
        api.complete(task.task_id)
        self.assertEqual(self.store.count_tasks("done"), 1)

        self.assertTrue(api.undo()["ok"])
        self.assertEqual(self.store.count_tasks("done"), 0)
        self.assertEqual(self.store.count_tasks("open"), 1)

    def test_archiving_one_can_be_undone(self):
        task = self.add("交作业")
        api = self.api()
        api.archive(task.task_id)
        self.assertEqual(self.store.count_tasks("archive"), 1)
        api.undo()
        self.assertEqual(self.store.count_tasks("archive"), 0)
        self.assertEqual(self.store.count_tasks("open"), 1)

    def test_bulk_actions_are_undoable_too(self):
        a = self.add("一")
        b = self.add("二")
        api = self.api()
        api.bulk("done", [a.task_id, b.task_id])
        self.assertEqual(self.store.count_tasks("done"), 2)
        result = api.undo()
        self.assertEqual(result["count"], 2)
        self.assertEqual(self.store.count_tasks("open"), 2)

    def test_nothing_to_undo_says_so(self):
        self.assertFalse(self.api().undo()["ok"])
