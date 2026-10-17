"""字和文件分开发：两种顺序都得记上（用户 2026-10-17 报的）。

真实群里的常见发法：
  · 先发一句"明天要交作业"，再发一张作业要求的图（文件后到）
  · 先甩一份 Word，再补一句"就是这个，明天交"（文件先到）

以前只做了前一种：文件先到的那种，**整份附件就只在消息表里躺着**，
界面上哪儿都找不到 —— 用户看到的正是"文件内容不符合就记不进去"。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run                                                      # noqa: E402
from app.contracts.models import SOURCE_QQ_GROUP, Message        # noqa: E402
from app.pipeline.chain import Pipeline                          # noqa: E402
from app.pipeline.parse_enricher import RuleParseEnricher        # noqa: E402
from app.sources.manual import build_attachment                  # noqa: E402
from app.store.repository import SqliteStore, open_database      # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.store = SqliteStore(open_database(self.home / "s.db"))
        self.pipeline = Pipeline(draft_enrichers=[RuleParseEnricher()])
        self.files = self.home / "作业要求.docx"
        self.files.write_bytes(b"PK fake docx")
        run.PENDING_FILES.clear()

    def tearDown(self):
        run.PENDING_FILES.clear()
        self.store.close()
        self.tmp.cleanup()

    def message(self, msg_id: str, text: str, with_file: bool = False) -> Message:
        return Message(
            msg_id=msg_id, source_kind=SOURCE_QQ_GROUP, source_name="某班群",
            sender="班长", text=text,
            attachments=[build_attachment(self.files)] if with_file else [])


class TestFileFirst(Base):
    def test_the_file_follows_the_words_that_come_after(self):
        # ① 先发文件：内容是一段"不像待办"的说明
        run.ingest_message(self.store, self.pipeline,
                           self.message("qq:101", "第一章 总则\n本要求适用于全体同学。",
                                        with_file=True))
        self.assertEqual(self.store.count_tasks("open"), 0)

        # ② 再补一句人话：这句会建出待办
        run.ingest_message(self.store, self.pipeline,
                           self.message("qq:102", "就是这个，明天下午 3 点在 612 交"))

        tasks = self.store.list_tasks("open")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(tasks[0].attachments), 1,
                         "文件先发的顺序也必须挂上 —— 这是用户报的那个坑")


class TestFileLast(Base):
    def test_the_old_direction_still_works(self):
        run.ingest_message(self.store, self.pipeline,
                           self.message("qq:201", "明天下午 3 点在 612 交作业"))
        run.ingest_message(self.store, self.pipeline,
                           self.message("qq:202", "要求见附件", with_file=True))
        tasks = self.store.list_tasks("open")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(tasks[0].attachments), 1)


class TestOrphanFile(Base):
    def test_a_file_nobody_claims_lands_in_the_junk_box(self):
        """谁也没来认领的那份：至少进废件箱，还能点「这条其实有用」捡回来。"""
        run.ingest_message(self.store, self.pipeline,
                           self.message("qq:301", "第一章 总则\n本要求适用于全体同学。",
                                        with_file=True))
        before = self.store.count_dropped()

        run._sweep_pending_files(self.store, 60, force=True)   # 相当于超时/收工
        rows = self.store.list_dropped()
        # 文字那几行本来就进过废件箱 → 不再重复记一笔（同一个文件出现两遍更难找）
        self.assertLessEqual(len(rows), max(before, 1))
        self.assertTrue(any(row["msg_id"] == "qq:301" for row in rows),
                        "要留着 msg_id，界面才能连文件一起捡回来")
