"""完整备份：库 + 配置 + 附件，一个 zip 能直接搬到另一台电脑。

每天那份只备数据库 —— 换了电脑，待办点开全是"文件不在了"。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import SOURCE_QQ_GROUP, Message, TaskDraft   # noqa: E402
from app.runtime import backup                                          # noqa: E402
from app.store.repository import SqliteStore, open_database             # noqa: E402


class TestFullBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._old = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = str(self.home)
        (self.home / "data").mkdir()
        self.store = SqliteStore(open_database(self.home / "data" / "secretary.db"))
        self.store.save_drafts(
            Message(msg_id="qq:1", source_kind=SOURCE_QQ_GROUP, source_name="群", text="x"),
            [TaskDraft(title="交作业")])
        self.store.close()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old
        self.tmp.cleanup()

    def test_the_zip_has_the_database_the_config_and_the_attachments(self):
        att = self.home / "data" / "attachments" / "2026-10-17"
        att.mkdir(parents=True)
        (att / "作业要求.docx").write_bytes(b"PK fake")
        (self.home / "config.toml").write_text("[push]\n", encoding="utf-8")

        path = backup.full_backup()
        self.assertIsNotNone(path, "完整备份没打出来")
        with zipfile.ZipFile(path) as bundle:
            names = bundle.namelist()
        self.assertIn("secretary.db", names)
        self.assertIn("config.toml", names)
        self.assertIn("attachments/2026-10-17/作业要求.docx", names)

    def test_it_keeps_only_the_latest_three_full_backups(self):
        for i in range(5):
            target = self.home / "data" / "backups"
            target.mkdir(parents=True, exist_ok=True)
            (target / f"完整备份-2026010{i}.zip").write_bytes(b"x")
        backup.full_backup()
        self.assertLessEqual(len(list((self.home / "data" / "backups")
                                      .glob("完整备份-*.zip"))), backup.FULL_KEEP)

    def test_the_summary_counts_attachment_files(self):
        att = self.home / "data" / "attachments"
        att.mkdir(parents=True)
        (att / "a.jpg").write_bytes(b"x" * 2048)
        self.assertEqual(backup.attachments_summary()["count"], 1)
