"""一键更新。

它要动的是**程序自己的文件**（exe + `_internal`），所以这里盯三件事：

  1. 挑的必须是补丁包，不能挑错成完整版或者别的附件；
  2. 生成的那个批处理**必须是纯 ASCII** —— 中文在 cmd 的代码页下容易变乱码，
     而这个脚本是在"程序已经关了"的时候跑的，出问题就没人能救；
  3. 它得先等旧进程退出、再覆盖、最后把程序打开（顺序错了就是白干）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.runtime import selfupdate                                 # noqa: E402


class TestPatchUrl(unittest.TestCase):
    """下载地址按版本号拼，**不问接口**。

    为什么：GitHub 的接口对未登录请求每小时只给 60 次，撞上限流时
    「检查更新」会报"连不上网"，让人以为是自己断网（2026-10-13 真发生）。
    """

    def test_builds_the_release_download_url(self):
        url = selfupdate.patch_url("owner/repo", "0.3.4")
        self.assertEqual(
            url,
            "https://github.com/owner/repo/releases/download/v0.3.4/"
            "personal-secretary-v0.3.4-patch.zip")

    def test_tolerates_a_leading_v(self):
        self.assertIn("/v0.9.9/", selfupdate.patch_url("o/r", "v0.9.9"))


class TestUpdaterScript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def text(self, **kwargs) -> str:
        script = selfupdate.updater_script(self.dir, 4242, **kwargs)
        # 用 ascii 读：不是纯 ASCII 就会在这儿炸
        return script.read_text(encoding="ascii")

    def test_script_is_pure_ascii(self):
        """批处理里出现中文就可能变乱码 —— 而这个脚本跑的时候没人能救场。"""
        self.assertTrue(self.text(restart_receiver=True).isascii())

    def test_order_wait_then_extract_then_start(self):
        text = self.text(restart_receiver=False)
        wait = text.index(":wait")
        extract = text.index("tar -xf")
        start = text.index('start ""')
        self.assertLess(wait, extract, "得先等旧进程退出，否则 exe 覆盖不了")
        self.assertLess(extract, start, "得先解压完再打开程序")
        self.assertIn("PID eq 4242", text)
        self.assertIn('cd /d "%~dp0"', text)        # 不写死路径（目录名里有中文）

    def test_restarts_the_background_only_if_it_was_running(self):
        self.assertNotIn("--serve", self.text(restart_receiver=False))
        self.assertIn("--serve", self.text(restart_receiver=True))

    def test_failure_is_recorded_not_silent(self):
        text = self.text(restart_receiver=False)
        self.assertIn(":failed", text)
        self.assertIn("FAILED", text)               # 失败要留痕，别静默
