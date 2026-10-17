"""导出到手机日历（.ics）。导出来得能被别的日历软件认，不然等于没做。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import SOURCE_QQ_GROUP, Message, TaskDraft   # noqa: E402
from app.sinks.ical import build                                        # noqa: E402
from app.store.repository import SqliteStore, open_database             # noqa: E402


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

    def add(self, title: str, due: str = "", location: str = ""):
        return self.store.save_drafts(self.message, [
            TaskDraft(title=title, due_at=due or None, location=location)])[0]


class TestBuild(Base):
    def test_only_tasks_with_a_time_become_events(self):
        self.add("10 分钟后开会", "2026-10-18 13:30")
        self.add("拿快递")
        text = build(self.store.list_tasks("open"))
        self.assertEqual(text.count("BEGIN:VEVENT"), 1)
        self.assertIn("SUMMARY:10 分钟后开会", text)
        self.assertNotIn("拿快递", text)
        self.assertTrue(text.startswith("BEGIN:VCALENDAR"))
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))

    def test_commas_and_newlines_are_escaped(self):
        """iCal 里逗号/分号/换行是语法，不转义会把后半行吃掉。"""
        task = self.add("买牛奶, 面包", "2026-10-18 09:00")
        task.quoted = "记一下\n第二行"
        text = build([task])
        self.assertIn("SUMMARY:买牛奶\\, 面包", text)
        self.assertIn("DESCRIPTION:记一下\\n第二行", text)

    def test_long_lines_are_folded_by_bytes(self):
        """一行超过 75 字节要折行 —— 中文一个字 3 字节，很容易超。"""
        task = self.add("很长的标题" * 30, "2026-10-18 09:00")
        text = build([task])
        for line in text.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75, line[:20])


class TestBridge(Base):
    def test_export_writes_a_file_and_says_where(self):
        from app.ui.bridge import Api

        self.add("交实验报告", "2026-10-19 20:00")
        res = Api(self.store, [], {}, notices=[]).export_ical()
        self.assertTrue(res["ok"], res)
        path = Path(res["path"])
        self.assertTrue(path.is_file())
        self.assertIn("交实验报告", path.read_text(encoding="utf-8"))

    def test_nothing_timed_says_so_instead_of_writing_an_empty_file(self):
        from app.ui.bridge import Api

        self.add("拿快递")
        res = Api(self.store, [], {}, notices=[]).export_ical()
        self.assertFalse(res["ok"])
        self.assertIn("没时间", res["message"])
