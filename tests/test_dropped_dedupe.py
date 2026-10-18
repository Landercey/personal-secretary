"""废件箱不再重复：同一条消息只记一次。

真事（2026-10-18）：补收/重连把同一条消息喂了 26 遍，废件箱里同一条
闲聊躺着 26 行；按消息编号去重后发现 1086 行里只有 ~760 条是不同消息。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.store.repository import SqliteStore, open_database   # noqa: E402


class TestDroppedDedupe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "s.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_same_message_is_only_recorded_once(self):
        store = SqliteStore(open_database(self.db))
        for _ in range(3):
            store.log_dropped_text("哈哈", "闲聊", "qq", "群", "qq:9")
        store.log_dropped_text("收到", "闲聊", "qq", "群", "qq:10")
        rows = store.list_dropped()
        self.assertEqual([r["text"] for r in rows], ["收到", "哈哈"])
        self.assertEqual({r["msg_id"] for r in rows}, {"qq:9", "qq:10"})
        store.close()

    def test_old_duplicate_rows_are_cleaned_on_open(self):
        store = SqliteStore(open_database(self.db))
        # 绕过闸门直接塞重复行，模拟老版本留下的数据
        with store.conn:
            for _ in range(5):
                store.conn.execute(
                    "INSERT INTO dropped_items (text, reason, msg_id, dropped_at)"
                    " VALUES ('旧数据', '闲聊', 'qq:1', '2026-10-17 08:00:00')")
            store.conn.execute("DELETE FROM meta WHERE key='dropped_dedup_v1'")
        store.close()

        again = SqliteStore(open_database(self.db))
        self.assertEqual(len(again.list_dropped()), 1)
        again.close()

    def test_rows_without_an_id_are_left_alone(self):
        store = SqliteStore(open_database(self.db))
        store.log_dropped_text("无编号", "闲聊", "manual", "", "")
        store.log_dropped_text("无编号", "闲聊", "manual", "", "")
        self.assertEqual(len(store.list_dropped()), 2)
        store.close()
