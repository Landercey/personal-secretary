"""AI 精修：规则先入库，AI 在后台线程里回来改。

盯四件事：
  1. 改完标题、留一笔「AI 精修：旧 → 新」；
  2. 用户改过的/已完成的，**一律不碰**（以用户为准）；
  3. 排队太多就熔断，短消息不问 —— 不为一个改动排一分钟队；
  4. 接收端不等它（`submit` 立刻返回，慢模型不堵收消息）。
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import SOURCE_QQ_GROUP, Message, TaskDraft   # noqa: E402
from app.runtime.ai_refine import Refiner                             # noqa: E402
from app.store.repository import SqliteStore, open_database           # noqa: E402


class FakeAI:
    """假的 AI 插件：要它说什么就说什么，还能装慢。"""

    name = "ai"
    order = 90

    def __init__(self, items=None, delay: float = 0.0, enabled: bool = True,
                 min_chars: int = 10, fail: bool = False) -> None:
        self.items = items or []
        self.delay = delay
        self._enabled = enabled
        self.min_chars = min_chars
        self.fail = fail
        self.calls = 0

    def enabled(self) -> bool:
        return self._enabled

    def wants(self, text: str) -> bool:
        return len(" ".join(str(text).split())) >= self.min_chars

    def answer(self, text: str) -> str:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise OSError("connection reset")
        return "[]"

    def drafts_from(self, answer, message, text):
        return [TaskDraft(title=item["title"], due_at=item.get("due"),
                          location=item.get("loc"), quoted="原话",
                          confidence=0.6) for item in self.items]

    def fallback(self, message, text):
        return TaskDraft(title=f"看一眼这条（{len(text)} 字，AI 没连上）")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SqliteStore(open_database(Path(self.tmp.name) / "r.db"))
        self.message = Message(msg_id="qq:1", source_kind=SOURCE_QQ_GROUP,
                               source_name="测试群",
                               text="明天下午三点在612开会，记得带上材料，别迟到")
        self.task = self._add("明天下午三点开会")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _add(self, title: str, due: str | None = "2026-10-18 15:00:00"):
        saved = self.store.save_drafts(
            self.message, [TaskDraft(title=title, due_at=due)])
        return saved[0]

    def _refiner(self, ai: FakeAI) -> Refiner:
        return Refiner(self.store, ai).start()

    def _wait(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False


class TestRefine(Base):
    def test_the_title_gets_better_and_the_change_is_logged(self):
        ai = FakeAI(items=[{"title": "开会（带材料）", "due": "2026-10-18 15:00"}])
        refiner = self._refiner(ai)
        try:
            self.assertTrue(refiner.submit(self.message, [self.task]))
            self.assertTrue(self._wait(
                lambda: self.store.get_task(self.task.task_id).title == "开会（带材料）"))
        finally:
            refiner.stop()

        task = self.store.get_task(self.task.task_id)
        self.assertFalse(task.ai_pending, "精修完要摘掉「AI 整理中…」")
        self.assertLess(task.confidence, 1.0, "改成 AI 版后要带【AI】标")
        events = [row["detail"] for row in self.store.conn.execute(
            "SELECT detail FROM task_events WHERE task_id=? AND kind='ai'",
            (self.task.task_id,))]
        self.assertTrue(events and "AI 精修：" in events[0], events)

    def test_a_task_the_user_edited_is_left_alone(self):
        """用户手动改过标题 → 以他为准，AI 回来也不许覆盖。"""
        ai = FakeAI(items=[{"title": "AI 版标题"}])
        self.store.update_fields(self.task.task_id, {"title": "我自己改的"})
        refiner = self._refiner(ai)
        try:
            refiner.submit(self.message, [self.task])
            time.sleep(0.3)
        finally:
            refiner.stop()
        self.assertEqual(self.store.get_task(self.task.task_id).title, "我自己改的")

    def test_a_completed_task_is_left_alone(self):
        ai = FakeAI(items=[{"title": "AI 版标题"}])
        self.store.set_status(self.task.task_id, "done")
        refiner = self._refiner(ai)
        try:
            refiner.submit(self.message, [self.task])
            time.sleep(0.3)
        finally:
            refiner.stop()
        self.assertEqual(self.store.get_task(self.task.task_id).title, "明天下午三点开会")


class TestGates(Base):
    def test_short_messages_do_not_go_to_ai(self):
        ai = FakeAI(items=[{"title": "AI 版"}], min_chars=200)
        refiner = self._refiner(ai)
        try:
            self.assertFalse(refiner.submit(self.message, [self.task]))
            time.sleep(0.2)
        finally:
            refiner.stop()
        self.assertEqual(ai.calls, 0, "短消息不该问模型（又慢又花钱）")

    def test_the_queue_breaks_the_circuit_when_it_is_full(self):
        """AI 慢的时候，堆超过 ai.queue_max 就不收了 —— 任务照常在列表里。"""
        from app.runtime.config import config

        old = config.get("ai", "queue_max", 6)
        config.set("ai", "queue_max", 1)
        ai = FakeAI(items=[{"title": "AI 版"}], delay=0.4)
        refiner = self._refiner(ai)
        try:
            self.assertTrue(refiner.submit(self.message, [self.task]))
            self.assertFalse(refiner.submit(self.message, [self.task]),
                             "队列满了还收，等于让所有人都等")
        finally:
            refiner.stop()
            config.set("ai", "queue_max", old)

    def test_the_receiver_does_not_wait_for_a_slow_model(self):
        """submit 必须立刻返回：收消息那条线程一秒都不能被 AI 占住。"""
        ai = FakeAI(items=[{"title": "AI 版"}], delay=1.0)
        refiner = self._refiner(ai)
        try:
            started = time.monotonic()
            refiner.submit(self.message, [self.task])
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertTrue(self.store.get_task(self.task.task_id).ai_pending,
                            "排队期间界面上要显示「AI 整理中…」")
        finally:
            refiner.stop()


class TestPipelineSkip(unittest.TestCase):
    def test_skip_ai_leaves_the_rule_result(self):
        """`skip={"ai"}` = 后台精修那条路：AI 零件整个不跑。"""
        from app.contracts.models import Message
        from app.pipeline.chain import Pipeline
        from app.pipeline.parse_enricher import RuleParseEnricher

        class Loud:
            name = "ai"
            order = 90
            called = False

            def enabled(self):
                return True

            def process(self, message, drafts):
                Loud.called = True
                return [TaskDraft(title="AI 版")]

        pipe = Pipeline(draft_enrichers=[RuleParseEnricher(), Loud()])
        message = Message(msg_id="m:1", source_kind=SOURCE_QQ_GROUP,
                          text="明天下午三点开会")
        _prepared, drafts = pipe.run(message, skip={"ai"})
        self.assertFalse(Loud.called)
        self.assertEqual([d.title for d in drafts], ["开会"])

        _prepared, drafts = pipe.run(message)
        self.assertTrue(Loud.called)
        self.assertEqual([d.title for d in drafts], ["AI 版"])
