"""M0：启动计数/安全模式、启动自检、日志里的 token 打码。

这三样都是"平时看不见、出事时救命"的：
  · 连续几次没起来 → 自动进安全模式（至少让用户进得去改设置）；
  · 库坏了 → 改名留档 + 用新库起来 + 告诉用户去备份恢复；
  · background.log 里的 access_token 打码 —— 用户会把整段日志贴到群里。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class StartupBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._old = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = str(self.home)
        (self.home / "data").mkdir()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old
        self.tmp.cleanup()


class TestStartupCounter(StartupBase):
    def test_attempts_accumulate_until_the_app_is_usable(self):
        from app.runtime import startup

        self.assertEqual(startup.note_attempt(), 1)
        self.assertEqual(startup.note_attempt(), 2)
        startup.mark_ready()                     # 界面起来了
        self.assertEqual(startup.crash_count(), 0)
        self.assertEqual(startup.note_attempt(), 1, "起来了就重新数")

    def test_the_third_failed_start_is_safe_mode(self):
        from app.runtime import startup

        startup.note_attempt()
        startup.note_attempt()
        third = startup.note_attempt()
        self.assertGreaterEqual(third, startup.LIMIT,
                                "连续没起来三次，下一次就该进安全模式")

    def test_safe_mode_only_rescues_once(self):
        """安全模式只救一次：进去之后清计数，下次必须正常启动。

        不清的话它会**永远**停在安全模式（插件一直不加载），比原来的问题更糟。
        另外 `--serve` 也必须清计数 —— 只在界面那条路清的话，
        每开一次（后台 + 界面各加一次）就攒 1，三次就把正常启动误判成"打不开"。
        """
        from app.runtime import startup

        for _ in range(startup.LIMIT):
            startup.note_attempt()
        startup.reset()                        # main() 进安全模式时做的事
        self.assertEqual(startup.note_attempt(), 1, "下次启动要重新从 1 数")

    def test_the_serve_path_also_clears_the_counter(self):
        """`--serve` 起来之后也要 mark_ready（不然会误进安全模式）。"""
        source = (ROOT / "run.py").read_text(encoding="utf-8")
        # run_qq 和 run_deploy 各一处
        self.assertGreaterEqual(source.count("startup.mark_ready()"), 3,
                                "界面 + 两条接收端路径都要清")


class TestPreflight(StartupBase):
    def test_a_healthy_db_produces_no_notes(self):
        from app.runtime import preflight
        from app.store.repository import SqliteStore, open_database

        db = self.home / "data" / "secretary.db"
        store = SqliteStore(open_database(db))
        store.close()
        self.assertEqual(preflight.check(db), [])

    def test_a_corrupt_db_is_moved_aside_not_deleted(self):
        from app.runtime import preflight

        db = self.home / "data" / "secretary.db"
        db.write_bytes(b"this is not a sqlite file" * 40)
        notes = preflight.check(db)

        self.assertTrue(notes, "库坏了必须留一条提示")
        self.assertTrue(any(n["code"] == "PREFLIGHT.DB.CORRUPT" for n in notes))
        self.assertFalse(db.exists(), "坏库要挪走，别让程序继续往坏文件里写")
        broken = list((self.home / "data").glob("secretary.broken-*.db"))
        self.assertEqual(len(broken), 1, "坏文件要留档，不能直接删")
        self.assertIn("备份", notes[0]["message"])


class TestTokenRedaction(unittest.TestCase):
    def test_the_console_banner_does_not_print_the_token(self):
        """background.log 里不能出现完整 token —— 用户会把日志整段贴出去。"""
        source = (ROOT / "run.py").read_text(encoding="utf-8")
        self.assertNotIn("?access_token={settings.token}", source)
        self.assertIn("access_token={'***'", source)
