"""子待办：只支持一层，子项不进任何分组，父待办上显示 +N。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import SOURCE_QQ_GROUP, Message, TaskDraft   # noqa: E402
from app.store.repository import SqliteStore, open_database             # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SqliteStore(open_database(Path(self.tmp.name) / "s.db"))
        self.message = Message(msg_id="qq:1", source_kind=SOURCE_QQ_GROUP,
                               source_name="某班群", text="x")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, title: str):
        return self.store.save_drafts(self.message, [TaskDraft(title=title)])[0]


class TestChildren(Base):
    def test_a_child_disappears_from_the_list_but_stays_under_its_parent(self):
        parent = self.add("搬家")
        kid = self.add("借推车")
        ok, why = self.store.attach_child(kid.task_id, parent.task_id)
        self.assertTrue(ok, why)

        titles = [t.title for t in self.store.list_tasks("open")]
        self.assertEqual(titles, ["搬家"], "子项不该再出现在列表里")
        self.assertEqual([t.title for t in self.store.children_of(parent.task_id)], ["借推车"])
        self.assertEqual(self.store.child_counts().get(parent.task_id), 1)
        self.assertEqual(self.store.count_tasks("open"), 1)

    def test_three_levels_are_allowed_and_the_fourth_is_refused(self):
        """一层不够用（搬家 → 借推车 → 找张叔），但再深就没人看得清了。"""
        a, b, c, d = (self.add(t) for t in ("搬家", "借推车", "找张叔", "打电话"))
        self.assertTrue(self.store.attach_child(b.task_id, a.task_id)[0])
        self.assertTrue(self.store.attach_child(c.task_id, b.task_id)[0])
        ok, why = self.store.attach_child(d.task_id, c.task_id)
        self.assertFalse(ok)
        self.assertIn("层", why)

    def test_the_whole_tree_shows_up_under_the_top_task(self):
        a, b, c = self.add("搬家"), self.add("借推车"), self.add("找张叔")
        self.store.attach_child(b.task_id, a.task_id)
        self.store.attach_child(c.task_id, b.task_id)
        tree = self.store.descendants_of(a.task_id)
        self.assertEqual([(t.title, depth) for t, depth in tree],
                         [("借推车", 1), ("找张叔", 2)])
        self.assertEqual(self.store.descendant_counts().get(a.task_id), 2)

    def test_it_refuses_to_be_its_own_parent(self):
        task = self.add("搬家")
        ok, _ = self.store.attach_child(task.task_id, task.task_id)
        self.assertFalse(ok)

    def test_it_can_be_moved_out_again(self):
        parent = self.add("搬家")
        kid = self.add("借推车")
        self.store.attach_child(kid.task_id, parent.task_id)
        self.assertTrue(self.store.detach_child(kid.task_id))
        self.assertEqual({t.title for t in self.store.list_tasks("open")}, {"搬家", "借推车"})

    def test_a_child_already_attached_is_refused(self):
        a, b, kid = self.add("搬家"), self.add("期末"), self.add("借推车")
        self.store.attach_child(kid.task_id, a.task_id)
        ok, why = self.store.attach_child(kid.task_id, b.task_id)
        self.assertFalse(ok)
        self.assertIn("已经是", why)
