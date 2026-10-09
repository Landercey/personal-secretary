"""界面上那几个"管后台"的能力：心跳、停止信号、开机自启、QQ 面板状态。

这些碰的都是**真实环境**（启动文件夹、data 目录），所以测试里格外小心：
只读的随便查，会改用户环境的必须先确认不会误伤。
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

from app.runtime import deploy, desktop, heartbeat              # noqa: E402
from app.store.repository import SqliteStore, open_database     # noqa: E402
from app.ui.bridge import Api                                   # noqa: E402


def fake_napcat() -> Path:
    """造一份跟真机上一样的目录结构，返回 shell 目录。"""
    shell = deploy.default_dir() / "NapCat.52230.Shell"
    (shell / "versions" / "9.9.33-52230" / "resources" / "app"
     / "napcat" / "config").mkdir(parents=True, exist_ok=True)
    (shell / "NapCatWinBootMain.exe").write_bytes(b"MZ")
    return shell


def write_qr(shell: Path, *, age_seconds: float = 0) -> Path:
    qr = deploy.qr_path(shell)
    qr.parent.mkdir(parents=True, exist_ok=True)
    qr.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(qr, (stamp, stamp))
    return qr


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = self.tmp.name
        self.store = SqliteStore(open_database(Path(self.tmp.name) / "t.db"))

    def tearDown(self):
        self.store.close()
        heartbeat.clear_stop()
        if self._old is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old
        self.tmp.cleanup()


class StopSignal(Base):
    """界面上的「停止后台」是写个文件让后台自己退，不是拿 PID 去杀。

    为什么不用 PID：得先确认那个 PID 确实是我们的后台进程，查错了就是误杀
    别人的程序。写文件让它自己看，简单也安全。
    """

    def test_request_then_see_it(self):
        self.assertFalse(heartbeat.stop_requested())
        heartbeat.request_stop()
        self.assertTrue(heartbeat.stop_requested())

    def test_clear_removes_it(self):
        heartbeat.request_stop()
        heartbeat.clear_stop()
        self.assertFalse(heartbeat.stop_requested())

    def test_clear_is_safe_when_nothing_is_there(self):
        heartbeat.clear_stop()
        heartbeat.clear_stop()
        self.assertFalse(heartbeat.stop_requested())

    def test_bridge_stop_writes_the_signal(self):
        Api(self.store).stop_background()
        self.assertTrue(heartbeat.stop_requested())


class Autostart(unittest.TestCase):
    """开机自启 = 往「启动」文件夹里放一个快捷方式。"""

    def test_startup_folder_is_under_appdata(self):
        folder = desktop.startup_folder()
        self.assertTrue(str(folder).lower().endswith("startup"))
        self.assertIn("microsoft", str(folder).lower())

    def test_entry_name_says_what_it_is(self):
        self.assertIn("开机自启", desktop.autostart_entry().name)

    def test_turning_it_off_when_it_is_already_off_is_fine(self):
        """只在**确实没开**的时候测 —— 免得把用户真开着的自启删掉。"""
        if desktop.autostart_enabled():
            self.skipTest("这台机器上开着开机自启，测试不碰它")
        ok, message = desktop.set_autostart(False)
        self.assertTrue(ok, message)


class QqPanel(Base):
    """「收消息」那一页要的状态。什么都还没装时也得能正常回话。"""

    def test_panel_works_before_anything_is_installed(self):
        data = Api(self.store).qq_panel()

        self.assertFalse(data["installed"])       # 还没装 NapCat
        self.assertFalse(data["qr"]["ready"])     # 也就没有二维码
        self.assertFalse(data["receiver"]["alive"])
        self.assertIsInstance(data["autostart"], bool)
        self.assertEqual(data["qr"]["image"], "")

    def test_qr_image_is_read_out_when_napcat_wrote_one(self):
        """二维码就是 NapCat 写的一个 PNG，界面直接读成 data URL 显示。"""
        write_qr(fake_napcat())
        data = Api(self.store).qq_panel()

        self.assertTrue(data["installed"])
        self.assertTrue(data["qr"]["ready"])
        self.assertTrue(data["qr"]["image"].startswith("data:image/png;base64,"))

    def test_stale_qr_is_not_handed_out(self):
        """过期的码不给图 —— 拿一个扫不上的码让人扫，比不给更气人。"""
        write_qr(fake_napcat(), age_seconds=900)
        data = Api(self.store).qq_panel()

        self.assertTrue(data["qr"]["ready"])
        self.assertEqual(data["qr"]["image"], "")
        self.assertGreater(data["qr"]["age_seconds"], 300)

    def test_connected_detail_comes_from_the_heartbeat(self):
        heartbeat.beat(self.store, "登录号 123456789｜收到 42", force=True)
        data = Api(self.store).qq_panel()

        self.assertTrue(data["receiver"]["alive"])
        self.assertIn("登录号", data["receiver"]["detail"])

    def test_autostart_toggle_answers_honestly(self):
        """不真去开关（会动用户的启动文件夹），只要它老实地回话。"""
        result = Api(self.store).set_autostart(desktop.autostart_enabled())
        self.assertIn("ok", result)
        self.assertIn("message", result)


class Wiring(Base):
    def test_serve_is_a_real_command(self):
        """开机自启和界面上的「启动 QQ」都调 --serve，它必须存在。"""
        import run as app

        self.assertTrue(callable(app.run_serve))

    def test_deploy_dir_follows_the_data_dir(self):
        from app.runtime.config import default_data_dir

        self.assertEqual(deploy.default_dir(), default_data_dir().parent / "napcat")

    def test_account_can_come_from_config(self):
        """界面上的「启动 QQ」不该逼用户记命令行参数。"""
        from app.runtime.config import DEFAULTS

        self.assertIn("account", DEFAULTS["qq"])


if __name__ == "__main__":
    unittest.main()
