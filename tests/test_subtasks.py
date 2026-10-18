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

    def test_children_can_be_included_when_asked(self):
        """提醒那边要拿到子项（它们也有自己的时间），列表默认不要。"""
        parent, kid = self.add("搬家"), self.add("借推车")
        self.store.attach_child(kid.task_id, parent.task_id)
        self.assertEqual(len(self.store.list_tasks("open")), 1)
        self.assertEqual(len(self.store.list_tasks("open", include_children=True)), 2)


class TestChildrenElsewhere(Base):
    def test_the_digest_tells_you_what_to_do_next(self):
        """推送：先列总任务，再写一行"现在轮到"。"""
        from app.sinks.console import build_digest

        parent = self.add("搬家")
        a = self.add("借推车")
        b = self.add("订车")
        self.store.attach_child(a.task_id, parent.task_id)
        self.store.attach_child(b.task_id, parent.task_id)
        rows = self.store.list_tasks("open")
        payload = build_digest(
            rows, children={parent.task_id: self.store.descendants_of(parent.task_id)})
        self.assertIn("搬家", payload.body)
        self.assertIn("现在轮到", payload.body)

    def test_search_shows_which_parent_a_child_belongs_to(self):
        from app.ui.bridge import Api

        parent, kid = self.add("搬家"), self.add("借推车")
        self.store.attach_child(kid.task_id, parent.task_id)
        items = Api(self.store, [], {}, notices=[]).search("借推车", "all")["items"]
        self.assertEqual(items[0]["parent"], "搬家")

    def test_a_child_goes_through_ai_refine_like_any_other_todo(self):
        """子项不是特殊物种：它就是一条普通待办，精修队列照样收。

        （用户问"消息录入才分进子待办，精修不是都有吗"——对，确实都有；
        这条测试把这件事钉住，免得以后谁在 submit 里按 parent_id 过滤。）
        """
        from app.runtime.ai_refine import Refiner

        class FakeAI:
            name = "ai"

            def enabled(self):
                return True

            def wants(self, text):
                return True

        parent, kid = self.add("搬家"), self.add("借推车")
        self.store.attach_child(kid.task_id, parent.task_id)
        refiner = Refiner(self.store, FakeAI())
        accepted = refiner.submit(self.message, [self.store.get_task(kid.task_id)])
        self.assertTrue(accepted, "子项被拒在精修队列外了")
        refiner.stop()


class TestGuessedTime(Base):
    def test_the_flag_survives_storage_and_edit_clears_it(self):
        """存进来带着"时间待补"，用户改完时间就该清掉。"""
        from app.ui.bridge import Api

        task = self.store.save_drafts(
            self.message, [TaskDraft(title="交报名表", due_at="2026-10-20 23:59:00",
                                     time_guess=True)])[0]
        self.assertTrue(self.store.get_task(task.task_id).time_guess)
        api = Api(self.store, [], {}, notices=[])
        self.assertTrue(api.edit_task(task.task_id, "交报名表", "2026-10-20T09:00", "")["ok"])
        self.assertFalse(self.store.get_task(task.task_id).time_guess)
        self.assertEqual(self.store.get_task(task.task_id).due_at, "2026-10-20 09:00:00")


class TestSplitAndMerge(Base):
    def test_one_message_with_two_events_becomes_two_todos(self):
        """一条通知里两件事（拉练 + 晚上学习理论），各有各的时间地点。"""
        from app.pipeline import quick_parse

        text = ("明天（10月18日）下午2:30进行校内拉练，（1、2营从西区操场到东区操场，"
                "3、4营从东区操场到西区操场，附路线图），两条路线同时进行，请辅导员做好拉练前的动员。"
                "拉练前，医生和巡逻的电话会发群里。"
                "明晚上训练时间在寝室内学习线上理论，目前学习情况不够理想，合格人数很少，"
                "绝大部分学生都没有开始学习，请大家抓紧时间学习！")
        drafts = quick_parse.parse_text(text, strict=True)
        self.assertEqual(len(drafts), 2, [d.title for d in drafts])
        self.assertTrue(drafts[0].due_at.endswith("14:30:00"), drafts[0].due_at)
        self.assertEqual(drafts[1].location, "寝室")

    def test_merging_two_todos_keeps_attachments_and_drops_the_source(self):
        a = self.add("领社保卡")
        b = self.add("带身份证")
        ok, why = self.store.merge_tasks(b.task_id, a.task_id)
        self.assertTrue(ok, why)
        self.assertEqual(self.store.get_task(b.task_id).status, "dropped")
        self.assertIn("合并自：带身份证", self.store.get_task(a.task_id).detail)
        self.assertEqual([t.title for t in self.store.list_tasks("open")], ["领社保卡"])

    def test_merging_a_child_into_its_parent_makes_it_disappear_from_the_tree(self):
        """子项并进父项：并完之后不能再挂在父项下面（否则看着像还在）。"""
        parent, kid = self.add("搬家"), self.add("借推车")
        self.store.attach_child(kid.task_id, parent.task_id)
        ok, why = self.store.merge_tasks(kid.task_id, parent.task_id)
        self.assertTrue(ok, why)
        self.assertEqual(self.store.children_of(parent.task_id), [])
        self.assertEqual(self.store.descendant_counts().get(parent.task_id), None)
        self.assertIn("合并自：借推车", self.store.get_task(parent.task_id).detail)
        self.assertEqual(self.store.get_task(kid.task_id).status, "dropped")

    def test_a_parent_cannot_be_merged_into_someone_else(self):
        """带子项的不能直接并走 —— 子树会跟着搬家，层级就乱了。"""
        parent, kid, other = self.add("搬家"), self.add("借推车"), self.add("期末")
        self.store.attach_child(kid.task_id, parent.task_id)
        ok, why = self.store.merge_tasks(parent.task_id, other.task_id)
        self.assertFalse(ok)
        self.assertIn("子项", why)
