"""第 4 步门禁（四）：多个线程共用同一个数据库连接。

真踩过的坑（而且是会让整个功能报废的那种）：
    QQ 消息在后台线程收，写库时抛
    "SQLite objects created in a thread can only be used in that same thread"
    表现是「消息显示收下了，但待办一条都没出现」——
    单线程测试完全测不出来。

所以这里必须真的开线程写。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import SOURCE_QQ_GROUP, Message, TaskDraft      # noqa: E402
from app.store.repository import SqliteStore, open_database               # noqa: E402


def make_message(index: int) -> Message:
    return Message(
        msg_id=f"qq:{index}",
        source_kind=SOURCE_QQ_GROUP,
        source_name="测试群",
        text=f"第 {index} 条",
    )


class TestCrossThreadAccess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = Path(self.tmp.name) / "t.db"
        self.store = SqliteStore(open_database(db))

    def tearDown(self):
        self.store.close()        # 不关的话 Windows 上临时目录删不掉
        self.tmp.cleanup()

    def test_write_from_a_background_thread(self):
        """最直白的一条：在主线程建连接，到子线程里写。"""
        errors: list[str] = []

        def worker():
            try:
                message = make_message(1)
                self.store.save_message(message)
                self.store.save_drafts(message, [TaskDraft(title="交作业")])
            except Exception as exc:
                # 记完整堆栈：并发 bug 只报一句 "TypeError: xxx" 是查不出来的，
                # 必须知道是哪一行。
                errors.append(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)

        self.assertEqual(errors, [], "子线程里写库失败了")
        self.assertEqual(self.store.stats()["tasks"], 1)

    def test_many_threads_writing_at_once(self):
        """十几个线程同时写，一条都不能丢、也不能报错。"""
        errors: list[str] = []
        per_thread = 10
        threads = []

        def worker(base: int):
            try:
                for i in range(per_thread):
                    index = base + i
                    message = make_message(index)
                    self.store.save_message(message)
                    self.store.save_drafts(message, [TaskDraft(title=f"事 {index}")])
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")

        for n in range(12):
            threads.append(threading.Thread(target=worker, args=(n * 1000,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [], "并发写出现异常")
        self.assertEqual(self.store.stats()["tasks"], 12 * per_thread)

    def test_read_while_another_thread_writes(self):
        """主线程一直在读、子线程一直在写，两边都不能出错。

        这就是真实场景：界面在主线程刷新列表，QQ 在后台线程往里塞。
        """
        errors: list[str] = []
        stop = threading.Event()

        def writer():
            try:
                for i in range(200):
                    message = make_message(i)
                    self.store.save_message(message)
                    self.store.save_drafts(message, [TaskDraft(title=f"事 {i}")])
                stop.set()
            except Exception as exc:
                errors.append(f"写:{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
                stop.set()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            while not stop.is_set():
                self.store.list_tasks("open")          # 边写边读
                self.store.count_tasks("open")
        except Exception as exc:
            errors.append(f"读:{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(self.store.count_tasks("open"), 200)

    def test_duplicate_key_still_works_across_threads(self):
        """去重逻辑在并发下也要照样生效。"""
        errors: list[str] = []

        def worker():
            try:
                for i in range(20):
                    message = make_message(i)
                    self.store.save_message(message)
                    self.store.save_drafts(message, [TaskDraft(title="同一件事")])
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        # 80 条"同一件事"只该留下 1 条
        self.assertEqual(self.store.stats()["tasks"], 1)


class TestConnectionWrapper(unittest.TestCase):
    """包装层本身的行为。"""

    def test_supports_context_manager_and_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_database(Path(tmp) / "x.db")
            try:
                with conn:
                    conn.execute("INSERT INTO meta (key, value) VALUES (?,?)", ("a", "1"))
                row = conn.execute("SELECT value FROM meta WHERE key=?", ("a",)).fetchone()
                self.assertEqual(row["value"], "1")
            finally:
                conn.close()

    def test_rolls_back_on_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_database(Path(tmp) / "y.db")
            try:
                with self.assertRaises(RuntimeError):
                    with conn:
                        conn.execute("INSERT INTO meta (key, value) VALUES (?,?)", ("b", "2"))
                        raise RuntimeError("中途出错")
                row = conn.execute("SELECT value FROM meta WHERE key=?", ("b",)).fetchone()
                self.assertIsNone(row, "出错后应该回滚")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
