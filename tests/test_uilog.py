"""界面侧的动作日志：窗口卡住时要留得下现场。

为什么专门写这个（2026-10-13 真踩过）：作者那边窗口"未响应"，我从外面查了
一圈 —— 没对话框、没联网、没在算、数据库也没被占 —— **一点线索都没有**，
因为界面这侧当时压根不写日志。只能硬关掉重开，然后猜。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.runtime import uilog                                    # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self._home = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = self.tmp.name
        self.addCleanup(self._restore_home)
        uilog._current.clear()          # 每个用例干净开始

    def _restore_home(self) -> None:
        if self._home is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._home

    def text(self) -> str:
        try:
            return uilog.path().read_text(encoding="utf-8")
        except OSError:
            return ""


class TestUiLog(Base):
    def test_normal_fast_call_stays_quiet(self):
        """正常的快调用**不写日志** —— 不然文件里全是噪音，真出事了翻不出来。"""
        uilog.start("qq_panel")
        uilog.finish("qq_panel")
        self.assertEqual(self.text(), "")

    def test_errors_are_recorded(self):
        uilog.start("start_qq")
        uilog.finish("start_qq", "OSError: 端口被占了")
        self.assertIn("✗ start_qq", self.text())
        self.assertIn("端口被占了", self.text())

    def test_watchdog_writes_down_a_call_that_never_comes_back(self):
        """**这条是重点**：窗口冻住的时候，看门狗线程照样在跑。"""
        uilog.start("sync_now")                     # 假装它卡住了（不调 finish）
        uilog.watch(threshold=0.05, every=0.05)
        for _ in range(60):                         # 最多等 3 秒
            time.sleep(0.05)
            if "卡住了" in self.text():
                break
        text = self.text()
        self.assertIn("卡住了", text)
        self.assertIn("sync_now", text)

    def test_finish_without_start_is_harmless(self):
        uilog.finish("没开始过")                     # 不许抛
        self.assertEqual(self.text(), "")
