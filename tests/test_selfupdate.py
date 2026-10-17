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


class TestDownloadRetries(unittest.TestCase):
    """下到一半断了要自己重试。

    37 MB 走代理传到一半断掉是常态（作者发版时自己撞过好几次 EOF）。
    断一次就报"下载失败"、让用户从头点一遍，体验比多等两秒差得多。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "update.zip"
        self._real = selfupdate.urllib.request.urlopen
        self._sleep = selfupdate.time.sleep
        selfupdate.time.sleep = lambda _s: None      # 别真等 2+4 秒

    def tearDown(self):
        selfupdate.urllib.request.urlopen = self._real
        selfupdate.time.sleep = self._sleep
        self.tmp.cleanup()

    def _fake(self, failures: int, payload: bytes = b"payload"):
        calls = {"n": 0}

        class Response:
            def __init__(self):
                self.headers = {"Content-Length": str(len(payload))}

            def read(self, _limit):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def urlopen(_request, timeout=0):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise OSError("Connection reset by peer")
            return Response()

        selfupdate.urllib.request.urlopen = urlopen
        return calls

    def test_a_dropped_connection_is_retried(self):
        calls = self._fake(failures=2)
        selfupdate.download("https://example.invalid/x.zip", self.dest)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(self.dest.read_bytes(), b"payload")

    def test_giving_up_after_three_tries(self):
        calls = self._fake(failures=99)
        with self.assertRaises(OSError):
            selfupdate.download("https://example.invalid/x.zip", self.dest)
        self.assertEqual(calls["n"], 3)


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


class TestUpgradeUrl(unittest.TestCase):
    """一键更新下的是「一键升级包」，地址同样按版本号拼、不查接口。"""

    def test_builds_the_upgrade_asset_url(self):
        self.assertEqual(
            selfupdate.upgrade_url("owner/repo", "0.3.4"),
            "https://github.com/owner/repo/releases/download/v0.3.4/"
            "personal-secretary-v0.3.4-upgrade.exe")

    def test_tolerates_a_leading_v(self):
        self.assertIn("upgrade.exe", selfupdate.upgrade_url("o/r", "v0.9.9"))


class TestRunUpgrader(unittest.TestCase):
    """更新交给升级包去做。

    为什么不再自己写批处理等 PID 再 tar（2026-10-17 实测）：detached 起 cmd 时，
    `tasklist | find` 那个管道会卡死 —— 程序被自己关了、文件没换成、也没重开。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self._real = selfupdate.subprocess.Popen
        self.calls = []

        def fake_popen(args, **kwargs):
            self.calls.append((args, kwargs))
            return object()

        selfupdate.subprocess.Popen = fake_popen
        self.addCleanup(setattr, selfupdate.subprocess, "Popen", self._real)

    def test_it_runs_the_package_against_the_install_folder(self):
        exe = self.dir / "updates" / "一键升级-v9.9.9.exe"
        app = self.dir / "私人秘书"
        selfupdate.run_upgrader(exe, app)

        args, kwargs = self.calls[0]
        self.assertEqual(args[0], str(exe))
        self.assertEqual(args[1], "--dir")
        self.assertEqual(args[2], str(app))
        # 不能 detached：升级窗口要把进度和失败原因显示给用户看
        self.assertNotIn("DETACHED_PROCESS", {k.upper() for k in kwargs})
        self.assertNotIn("creationflags", kwargs)
