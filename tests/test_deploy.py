"""第 8 步门禁：一键部署的路径与配置逻辑。

下载和跑安装器没法离线测（要 300 MB 和几分钟），但**最容易出错的那部分能测**：
找目录、拼配置文件名、写 JSON。这三个地方错了，用户看到的现象都是
"装好了但一直连不上"，最难查。

路径结构照抄 2026-10-09 真机装出来的那份：
    NapCat.52230.Shell/versions/9.9.33-52230/resources/app/napcat/config/
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.runtime import deploy                                    # noqa: E402


def fake_install(base: Path, version: str = "9.9.33-52230",
                 build: int = 52230) -> Path:
    """造一份跟真机一样的目录结构。"""
    shell = base / f"NapCat.{build}.Shell"
    (shell / "versions" / version / "resources" / "app" / "napcat" / "config").mkdir(
        parents=True)
    (shell / "NapCatWinBootMain.exe").write_bytes(b"MZ")
    return shell


class TestFindDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_not_installed_when_empty(self):
        self.assertFalse(deploy.is_installed(self.base))
        self.assertIsNone(deploy.find_shell_dir(self.base))

    def test_finds_the_newest_shell(self):
        """装过两次（版本不同）时，取版本号大的那个。"""
        fake_install(self.base, build=49738)
        newest = fake_install(self.base, build=52230)
        self.assertEqual(deploy.find_shell_dir(self.base), newest)

    def test_config_dir_follows_version_folder(self):
        shell = fake_install(self.base)
        expected = shell / "versions" / "9.9.33-52230" / "resources" / "app" / "napcat" / "config"
        self.assertEqual(deploy.config_dir(shell), expected)

    def test_config_dir_falls_back_when_version_folder_missing(self):
        """万一 NapCat 以后改了目录结构，别直接崩，退到一个合理位置。"""
        shell = self.base / "NapCat.99999.Shell"
        shell.mkdir(parents=True)
        self.assertEqual(deploy.config_dir(shell), shell / "config")

    def test_installed_requires_launcher_and_config_dir(self):
        shell = fake_install(self.base)
        self.assertTrue(deploy.is_installed(self.base))
        (shell / "NapCatWinBootMain.exe").unlink()
        self.assertFalse(deploy.is_installed(self.base))


class TestWriteConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.shell = fake_install(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_filename_carries_the_account(self):
        """回归：文件名必须带 QQ 号。

        NapCat 按登录的号选配置文件；号对不上，它自己新建一份**空的**，
        然后永远连不上我们。这是实测踩过的坑。
        """
        path = deploy.write_onebot_config(self.shell, 123456789, "tok", 18080)
        self.assertEqual(path.name, "onebot11_123456789.json")
        self.assertTrue(path.is_file())

    def test_config_points_at_our_port_and_token(self):
        path = deploy.write_onebot_config(self.shell, 12345, "abc-123", 18081)
        data = json.loads(path.read_text(encoding="utf-8"))
        client = data["network"]["websocketClients"][0]
        self.assertEqual(client["url"],
                         "ws://127.0.0.1:18081/onebot/v11/ws?access_token=abc-123")
        self.assertEqual(client["token"], "abc-123")
        self.assertTrue(client["enable"])

    def test_report_self_message_on(self):
        """必须收自己发的消息 —— 小号收件箱（微信/短信截图转发）靠它。"""
        path = deploy.write_onebot_config(self.shell, 12345, "t", 18080)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(
            data["network"]["websocketClients"][0]["reportSelfMessage"])

    def test_rewriting_is_idempotent(self):
        """重复部署不该把配置写乱。"""
        first = deploy.write_onebot_config(self.shell, 12345, "t", 18080)
        text = first.read_text(encoding="utf-8")
        deploy.write_onebot_config(self.shell, 12345, "t", 18080)
        self.assertEqual(first.read_text(encoding="utf-8"), text)

    def test_writes_where_napcat_reads(self):
        path = deploy.write_onebot_config(self.shell, 12345, "t", 18080)
        self.assertEqual(path.parent, deploy.config_dir(self.shell))


class TestLaunch(unittest.TestCase):
    def test_with_account_tries_quick_login(self):
        cmd = deploy.launch_command(Path("X:/shell"), 123456789)
        self.assertEqual(cmd[1], "123456789")

    def test_without_account_goes_straight_to_qr(self):
        cmd = deploy.launch_command(Path("X:/shell"))
        self.assertEqual(len(cmd), 1)

    def test_qr_path_lives_next_to_config(self):
        shell = Path("X:/NapCat.1.Shell")
        self.assertEqual(deploy.qr_path(shell).parent.name, "cache")


class TestDownloadFallback(unittest.TestCase):
    def test_tries_every_mirror_before_giving_up(self):
        """直连下不动要换镜像 —— 实测 GitHub 在国内很慢。"""
        tried: list[str] = []
        original = deploy.download

        def boom(url, dest, on_progress=None, timeout=120.0):
            tried.append(url)
            raise OSError("下不动")

        deploy.download = boom
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(RuntimeError):
                    deploy.download_onekey(Path(tmp), None)
        finally:
            deploy.download = original

        self.assertEqual(len(tried), 1 + len(deploy.ONEKEY_MIRRORS))
        self.assertTrue(any("ghfast.top" in url for url in tried),
                        "没试 ghfast.top 镜像")

    def test_stops_at_first_success(self):
        tried: list[str] = []
        original = deploy.download

        def ok(url, dest, on_progress=None, timeout=120.0):
            tried.append(url)
            dest.write_bytes(b"x")
            return dest

        deploy.download = ok
        try:
            with tempfile.TemporaryDirectory() as tmp:
                deploy.download_onekey(Path(tmp), None)
        finally:
            deploy.download = original
        self.assertEqual(len(tried), 1, "第一个成功了就别再试后面的")


if __name__ == "__main__":
    unittest.main()
