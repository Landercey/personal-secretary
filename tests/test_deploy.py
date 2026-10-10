"""第 8 步门禁：一键部署的路径与配置逻辑。

下载和跑安装器没法离线测（要 300 MB 和几分钟），但**最容易出错的那部分能测**：
找目录、拼配置文件名、写 JSON。这三个地方错了，用户看到的现象都是
"装好了但一直连不上"，最难查。

路径结构照抄 2026-10-09 真机装出来的那份：
    NapCat.52230.Shell/versions/9.9.33-52230/resources/app/napcat/config/
"""

from __future__ import annotations

import json
import os
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


class TestStartShell(unittest.TestCase):
    """起 NapCat：窗口藏起来，但**输出必须有个去处**。

    为什么值得测：窗口藏掉之后子进程的标准输出会变成空句柄，
    Node 写的程序（NapCat 就是）往里写会直接报错退出 —— 现象是
    "点启动，几秒后 QQ 掉线"，而且一点错误信息都没有。
    """

    def test_output_goes_into_the_log_file(self):
        import subprocess as sp

        code = "import sys; print('NAPCAT-OK'); sys.stderr.write('ERR-OK\\n')"
        original = deploy.launch_command
        deploy.launch_command = lambda shell, qq=None: [sys.executable, "-c", code]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / "napcat.log"
                proc = deploy.start_shell(Path(tmp), log_path=log)
                proc.wait(timeout=30)
                text = log.read_text(encoding="utf-8", errors="replace")
        finally:
            deploy.launch_command = original

        self.assertIn("NAPCAT-OK", text)
        self.assertIn("ERR-OK", text, "stderr 也要收进去，别漏")

    def test_window_is_hidden_and_output_is_wired(self):
        """藏窗口 + 接输出，两件事都真的传给了 Popen（缺一个都会出乱码窗口/静默退出）。"""
        import subprocess as sp

        captured: dict = {}
        real = deploy.subprocess.Popen
        deploy.subprocess.Popen = lambda cmd, **kw: captured.update(kw) or None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                deploy.start_shell(Path(tmp), log_path=Path(tmp) / "l.log")
        finally:
            deploy.subprocess.Popen = real

        expected = (deploy.no_window_flags()
                    | getattr(sp, "CREATE_NEW_PROCESS_GROUP", 0))
        self.assertEqual(captured.get("creationflags"), expected)
        self.assertIn("stdout", captured)
        self.assertEqual(captured.get("stderr"), sp.STDOUT)


class TestProgressFile(unittest.TestCase):
    """装 NapCat 时的进度：后台进程写、界面读，只能靠这个文件。

    为什么值得测："装的过程界面上一动不动"是用户最容易以为卡死的地方，
    而这两头的路径一旦对不上（换个目录、换个文件名），**不会报任何错**，
    只会表现为"进度不显示" —— 静默失败，最难查。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._old = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = str(self.home)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old
        self.tmp.cleanup()

    def test_write_then_read(self):
        deploy.report("正在装 NapCat")
        data = deploy.read_progress()
        self.assertTrue(data["active"])
        self.assertEqual(data["text"], "正在装 NapCat")
        self.assertIn("started", data)

    def test_started_time_is_kept_across_reports(self):
        """连着报好几条时，"这轮从什么时候开始"不能被刷掉（不然"已等多久"永远归零）。"""
        deploy.report("第一步")
        first = deploy.read_progress()["started"]
        deploy.report("第二步")
        self.assertEqual(deploy.read_progress()["started"], first)

    def test_done_clears_active(self):
        deploy.report("在装")
        deploy.report("装好了", active=False)
        self.assertFalse(deploy.read_progress()["active"])

    def test_no_file_means_no_progress(self):
        self.assertEqual(deploy.read_progress(), {})

    def test_windows_hides_the_console(self):
        """装 QQ / 起 NapCat 都不许蹦黑框（用户会当病毒）。

        非 Windows 上取不到这个标志，返回 0 也能跑。
        """
        import subprocess as sp

        self.assertEqual(deploy.no_window_flags(),
                         getattr(sp, "CREATE_NO_WINDOW", 0))


if __name__ == "__main__":
    unittest.main()
