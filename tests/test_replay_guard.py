"""同一条消息只处理一次。

真事：每次启动都会"补收"最近的历史，断线重连也会重发。以前每条都重新跑
理解层 —— 同一条待办被记一次"合并重复"、同一条闲聊被记一次"拦下"。
今天实测 6 条待办被记了 104 次重复、废件箱 291 条。数字全是被重放吹大的。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run                                                    # noqa: E402
from app.contracts.models import SOURCE_QQ_GROUP, Message      # noqa: E402
from app.pipeline.chain import Pipeline                        # noqa: E402
from app.pipeline.parse_enricher import RuleParseEnricher      # noqa: E402
from app.store.repository import SqliteStore, open_database    # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.store = SqliteStore(open_database(self.home / "s.db"))
        self.pipeline = Pipeline(draft_enrichers=[RuleParseEnricher()])
        run.PENDING_FILES.clear()

    def tearDown(self):
        run.PENDING_FILES.clear()
        self.store.close()
        self.tmp.cleanup()

    def message(self, msg_id: str, text: str) -> Message:
        return Message(msg_id=msg_id, source_kind=SOURCE_QQ_GROUP,
                       source_name="某班群", sender="班长", text=text)

    def merged_events(self) -> int:
        row = self.store.conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='merged'").fetchone()
        return int(row[0])


class TestReplayGuard(Base):
    def test_the_same_message_twice_creates_one_task_and_zero_merges(self):
        self.assertEqual(run.ingest_message(
            self.store, self.pipeline, self.message("qq:1", "明天下午三点交实验报告")), 1)
        again = run.ingest_message(
            self.store, self.pipeline, self.message("qq:1", "明天下午三点交实验报告"))
        self.assertEqual(again, 0)
        self.assertEqual(self.store.count_tasks("open"), 1)
        self.assertEqual(self.merged_events(), 0, "重放不该被算成「重复出现」")

    def test_a_genuinely_new_message_still_goes_through(self):
        run.ingest_message(self.store, self.pipeline, self.message("qq:1", "明天下午三点交实验报告"))
        self.assertEqual(run.ingest_message(
            self.store, self.pipeline, self.message("qq:2", "后天上午九点体检")), 1)
        self.assertEqual(self.store.count_tasks("open"), 2)

    def test_a_real_repeat_is_still_counted_once(self):
        """同一个人把同一件事**又发了一次**（不同消息编号）算一次重复。"""
        run.ingest_message(self.store, self.pipeline, self.message("qq:1", "明天下午三点交实验报告"))
        run.ingest_message(self.store, self.pipeline, self.message("qq:2", "明天下午三点交实验报告"))
        self.assertEqual(self.store.count_tasks("open"), 1)
        self.assertEqual(self.merged_events(), 1)

    def test_no_id_never_matches(self):
        self.assertFalse(self.store.has_message(""))
        self.assertFalse(self.store.has_message(None))
