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


class StopSignalIsClearedOnStart(unittest.TestCase):
    """后台启动时必须清掉上一次的停止请求。

    **这是一次真事故**（用户报的："第一次登上了、左下角也显示了，然后突然退了"）：
    他早先点过「停止后台」，那个文件一直躺在 data/ 里；之后每次启动后台，
    等扫码的循环一开头就看到它 —— 登录成功后两秒自己退出，还把 NapCat 杀了。
    原来只有命令行那条路（`--qq`）清了，界面按钮走的 `--serve` 没清。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = self.tmp.name

    def tearDown(self):
        heartbeat.clear_stop()
        if self._old is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old
        self.tmp.cleanup()

    def test_prelude_clears_a_leftover_stop(self):
        import run

        heartbeat.request_stop()
        self.assertTrue(heartbeat.stop_requested())

        run.background_prelude()

        self.assertFalse(heartbeat.stop_requested(),
                         "上一次的停止请求没清掉，后台一起来就会自己退")

    def test_serve_calls_the_prelude(self):
        """`--serve` 走的就是这条路（界面那个按钮起的也是它）。"""
        import inspect

        import run

        source = inspect.getsource(run.run_serve)
        self.assertIn("background_prelude()", source)


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

    def test_restarting_throws_the_old_code_away(self):
        """重启后台时先把旧码删掉。

        不删的两个后果（用户报的）：界面上还挂着那张作废的码；
        而且"年龄"按文件时间算，刚重启完就显示"已经 N 分钟，多半过期了"。
        """
        shell = fake_napcat()
        qr = write_qr(shell, age_seconds=600)

        deploy.clear_qr(shell)

        self.assertFalse(qr.exists())
        data = Api(self.store).qq_panel()
        self.assertFalse(data["qr"]["ready"], "旧码没删干净，界面还会把它端出来")

    def test_clearing_a_code_that_never_existed_is_fine(self):
        deploy.clear_qr(fake_napcat())       # 第一次装：本来就没有码

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


class Preflight(Base):
    """启动 QQ 之前必须**提前查**。

    用户报的（确实是这样）：没填登录号时，`run_serve` 会**跳过整套 NapCat 部署**
    直接进"等 NapCat 连上来"的循环 —— 而那个 NapCat 永远不会启动。
    现象就是"卡住"，而且没有任何提示（输出还被丢进了 DEVNULL）。
    """

    def set_config(self, **qq):
        import importlib

        from app.runtime.config import Config

        module = importlib.import_module("app.runtime.config")
        self._original = getattr(self, "_original", module.config)
        module.config = Config({"qq": qq}, Path(self.tmp.name) / "config.toml")

    def tearDown(self):
        # ⚠️ 必须还原全局 config。不还原的话，后面所有读 config 的测试
        # （调度器的内存闸门、排班表的"我那一行"）都会看到一个假的默认配置 ——
        # 这个泄漏真发生过：两个跟本文件毫无关系的测试红了。
        import importlib

        if hasattr(self, "_original"):
            importlib.import_module("app.runtime.config").config = self._original
        super().tearDown()

    def test_no_blockers_when_everything_is_filled(self):
        self.set_config(enabled=True, account=10001, group_ids=[1])
        self.assertEqual(Api(self.store)._qq_blockers(), [])

    def test_missing_account_blocks_and_says_where_to_fill(self):
        self.set_config(enabled=True, account=0, group_ids=[1])
        problems = Api(self.store)._qq_blockers()

        self.assertEqual(len(problems), 1)
        self.assertIn("QQ 号", problems[0])
        self.assertIn("设置", problems[0])          # 得告诉他去哪儿填

    def test_disabled_blocks(self):
        self.set_config(enabled=False, account=10001)
        self.assertIn("还没打开", Api(self.store)._qq_blockers()[0])

    def test_both_missing_reports_both(self):
        self.set_config(enabled=False, account=0)
        self.assertEqual(len(Api(self.store)._qq_blockers()), 2)

    def test_start_refuses_instead_of_hanging(self):
        """**不能真去起进程** —— 那正是"卡住"的来源。它得先拒绝。"""
        self.set_config(enabled=True, account=0, group_ids=[1])
        result = Api(self.store).start_qq()

        self.assertFalse(result["ok"])
        self.assertTrue(result["blockers"])

    def test_panel_carries_the_blockers(self):
        self.set_config(enabled=True, account=0, group_ids=[1])
        self.assertTrue(Api(self.store).qq_panel()["blockers"])


class BackgroundLog(Base):
    """后台进程的输出要留档 —— 不然"点了没反应"只能靠猜。"""

    def test_stale_log_is_not_shown_when_it_is_running(self):
        heartbeat.beat(self.store, "登录号 1｜收到 2", force=True)
        self.assertEqual(Api(self.store).qq_panel()["log_tail"], [])

    def test_log_tail_is_shown_when_it_is_not_running(self):
        import importlib

        module = importlib.import_module("app.ui.bridge")
        log = module._background_log()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("第一行\n\n最后一行\n", encoding="utf-8")

        tail = Api(self.store).qq_panel()["log_tail"]
        self.assertEqual(tail, ["第一行", "最后一行"])   # 空行不要，尾巴保留

    def test_missing_log_is_not_an_error(self):
        self.assertEqual(Api(self.store).qq_panel()["log_tail"], [])


class MemoryReadout(Base):
    """左下角要实时显示"这东西吃多少"。"""

    def test_self_rss_is_a_plausible_number(self):
        from app.runtime.sysmon import self_rss_mb

        value = self_rss_mb()
        self.assertIsNotNone(value)
        self.assertGreater(value, 5)        # 光解释器就不止 5 MB
        self.assertLess(value, 2000)

    def test_summary_reports_own_memory(self):
        from app.runtime.sysmon import self_rss_mb

        summary = Api(self.store).config_summary()
        self.assertEqual(summary["ui_mb"], self_rss_mb())
        self.assertIsNone(summary["background_mb"])      # 后台没跑，就没有这个数

    def test_background_memory_comes_from_the_heartbeat(self):
        heartbeat.beat(self.store, "登录号 1", force=True)
        summary = Api(self.store).config_summary()

        self.assertIsNotNone(summary["background_mb"])
        self.assertGreater(summary["background_mb"], 0)


if __name__ == "__main__":
    unittest.main()
