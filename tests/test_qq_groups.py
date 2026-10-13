"""读取"我加了哪些群"这条链路，以及顺带修的那个回执 bug。

为什么要有这个功能：群号一个个手填是最烦的一步 —— 还得先去 QQ 里把群号找出来。
后台问 QQ 要一份列表（OneBot 的 `get_group_list`），界面拿它做勾选。

顺带修的 bug 更要紧：`_resolve_pending`（把 NapCat 的回执配回请求）**从来没被调用过**，
所以 `call_api` 永远等不到回执 —— 按 file_id 取附件那条路会死等 25 秒然后放弃，
而且那 25 秒里后面的消息全堵在它后头。下面 test_reply_is_... 就是钉它的。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import TIME_FORMAT                     # noqa: E402
from app.runtime import heartbeat                                # noqa: E402
from app.runtime import deploy                                   # noqa: E402
from app.runtime.config import Config                            # noqa: E402
from app.sources.qq import QQSettings, QQSource, group_entries    # noqa: E402
from app.ui.bridge import Api                                    # noqa: E402


class FakeStore:
    """够 heartbeat 用的最小 store（真的那个是 sqlite，这里只存一条 meta）。"""

    def __init__(self) -> None:
        self.meta: dict[str, str] = {}

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value

    def get_meta(self, key: str, default: str = "") -> str:
        return self.meta.get(key, default)


class FakeFuture:
    """只要 done()/set_result() —— `_resolve_pending` 只用这两个。"""

    def __init__(self) -> None:
        self.result = None

    def done(self) -> bool:
        return self.result is not None

    def set_result(self, value) -> None:
        self.result = value


class TestGroupEntries(unittest.TestCase):
    def test_normal_reply(self):
        entries = group_entries({"status": "ok", "data": [
            {"group_id": 222, "group_name": "学院通知"},
            {"group_id": 111, "group_name": "计算机2301班"},
        ]})
        self.assertEqual(sorted(g["id"] for g in entries), [111, 222])
        # 名字原样带出来。（排序按字符编码走 —— 中文名排不出"拼音序"，
        # 只要**稳定**就行：同一次读出来的顺序两次点开是一样的。）
        self.assertEqual({g["name"] for g in entries}, {"学院通知", "计算机2301班"})

    def test_missing_name_goes_last_and_stays_empty(self):
        entries = group_entries({"data": [
            {"group_id": 3},
            {"group_id": 1, "group_name": "有名字"},
        ]})
        self.assertEqual([g["id"] for g in entries], [1, 3])
        # 名字拿不到就留空 —— **不许编**：编错的名字比没有名字更容易点错群
        self.assertEqual(entries[1]["name"], "")

    def test_garbage_is_ignored_not_raised(self):
        for reply in (None, {}, {"data": None}, {"data": "x"},
                      {"data": [None, "y", {}, {"group_id": 0}, {"group_id": "不是号"}]}):
            with self.subTest(reply=reply):
                self.assertEqual(group_entries(reply), [])


class TestRefreshKeepsTheOldList(unittest.TestCase):
    def make(self) -> QQSource:
        return QQSource(QQSettings(enabled=True, group_ids=[1]))

    def test_question_failed_keeps_what_we_had(self):
        """问不到就保留上一次的列表 —— 别把用户已经看到的东西清空。"""
        source = self.make()
        source._groups = [{"id": 1, "name": "旧列表"}]

        async def dead(_action, _params, timeout=20.0):
            return None

        source.call_api = dead                                   # type: ignore[assignment]
        asyncio.run(source._refresh_groups())
        self.assertEqual(source.groups(), [{"id": 1, "name": "旧列表"}])

    def test_question_answered_replaces_the_list(self):
        source = self.make()

        async def alive(_action, _params, timeout=20.0):
            return {"data": [{"group_id": 9, "group_name": "新群"}]}

        source.call_api = alive                                  # type: ignore[assignment]
        asyncio.run(source._refresh_groups())
        self.assertEqual(source.groups(), [{"id": 9, "name": "新群"}])


class TestReplyIsNotAMessage(unittest.TestCase):
    """回执必须被摘出去：既不能当消息处理，也不能记进"收到几条"。"""

    def setUp(self):
        self.source = QQSource(QQSettings(enabled=True, group_ids=[1]))

    def test_pending_reply_resolves_and_is_not_counted(self):
        future = FakeFuture()
        self.source._pending["secretary-abc"] = future            # type: ignore[assignment]
        message, reason = self.source.feed_event(
            {"status": "ok", "retcode": 0, "echo": "secretary-abc",
             "data": [{"group_id": 5, "group_name": "班群"}]})
        self.assertIsNone(message)
        self.assertEqual(reason, "接口回执")
        self.assertIsNotNone(future.result, "回执没被填进请求 —— call_api 会一直等到超时")
        self.assertEqual(self.source.status()["received"], 0, "回执不算收到消息")

    def test_someone_elses_echo_is_not_ours(self):
        future = FakeFuture()
        self.source._pending["secretary-abc"] = future            # type: ignore[assignment]
        message, reason = self.source.feed_event(
            {"status": "ok", "echo": "别人的", "post_type": "message"})
        self.assertIsNone(future.result)          # 不是我们的回执，别乱填
        self.assertNotEqual(reason, "接口回执")


class TestHeartbeatCarriesGroups(unittest.TestCase):
    def test_groups_ride_along(self):
        store = FakeStore()
        heartbeat.beat(store, "在跑", groups=[{"id": 1, "name": "班群"}], force=True)
        state = heartbeat.state(store)
        self.assertTrue(state["alive"])
        self.assertEqual(state["groups"], [{"id": 1, "name": "班群"}])

    def test_groups_survive_a_stale_heartbeat(self):
        """后台刚停，那份列表照样能用 —— 用户还能在设置里挑群。"""
        store = FakeStore()
        store.meta[heartbeat.KEY] = json.dumps(
            {"at": "2000-01-01 00:00:00", "detail": "", "groups": [{"id": 2, "name": "旧"}]},
            ensure_ascii=False)
        state = heartbeat.state(store)
        self.assertFalse(state["alive"])
        self.assertEqual(state["groups"], [{"id": 2, "name": "旧"}])


class TestBridge(unittest.TestCase):
    def test_no_background_says_so_instead_of_an_empty_list(self):
        result = Api(None).qq_groups()          # 没有 store = 后台肯定没在跑
        self.assertEqual(result["groups"], [])
        self.assertFalse(result["alive"])

    def test_selected_comes_from_the_config(self):
        import importlib

        from app.runtime.config import Config

        module = importlib.import_module("app.runtime.config")
        original = module.config
        module.config = Config({"qq": {"group_ids": [123, 456]}})
        self.addCleanup(setattr, module, "config", original)

        self.assertEqual(Api(None).qq_groups()["selected"], [123, 456])


class TestPreciseLiveness(unittest.TestCase):
    """判断"后台进程还在不在"必须用另一把尺子。

    心跳的 `alive` = "最近 60 秒内有没有心跳"，进程退了它还能 True 一分钟。
    「重新出码」的流程正好踩在那一分钟上（用户 2026-10-13 报的三个症状同一原因）：
    界面等不到"它退了"→ 启动那步又因为同一分钟而拒绝 → 变成"后台没在跑"，
    顺手把"删旧码"那步也跳过了 → 旧码不消失、手机上确认了也没人接。

    精确的判据是那把独占锁：锁跟着进程走，进程一死系统立刻放锁。
    测试用 SECRETARY_HOME 指到临时目录，**不碰真的那把锁**。
    """

    def setUp(self):
        # 见 test_qq_control.Base 里的说明：Api 的后台线程可能晚一步往
        # 临时目录里写缓存，收尾时报"目录不是空的"。
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self._home = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = self.tmp.name
        self.addCleanup(self._restore_home)
        heartbeat.release_single_instance()
        self.addCleanup(heartbeat.release_single_instance)

    def _restore_home(self) -> None:
        if self._home is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._home

    def test_nobody_has_ever_run(self):
        self.assertFalse(heartbeat.proc_alive())

    def test_holder_counts_as_running(self):
        self.assertTrue(heartbeat.claim_single_instance())
        self.assertTrue(heartbeat.proc_alive(), "锁被占着就该说在跑")

    def test_released_means_gone(self):
        heartbeat.claim_single_instance()
        heartbeat.release_single_instance()
        self.assertFalse(heartbeat.proc_alive(), "锁放掉了就该说没在跑")

    def test_start_qq_refuses_with_the_precise_judgement(self):
        module = importlib.import_module("app.runtime.config")
        original = module.config
        module.config = Config({"qq": {"enabled": True, "account": 10001,
                                       "group_ids": [1]}})
        self.addCleanup(setattr, module, "config", original)

        heartbeat.claim_single_instance()          # 假装后台正跑着
        result = Api(None).start_qq()
        self.assertFalse(result["ok"])
        self.assertIn("已经在跑", result["message"])


class TestNapcatLoginErrorIsVisible(unittest.TestCase):
    """登录失败的原因必须能从界面看出来。

    真事（2026-10-13）：用户"还是登不上"，界面上一声不吭。实际原因在 NapCat
    自己的日志里 —— `当前账号(123456789)已登录,无法重复登录`：他配的那个号
    正登在自己电脑的普通 QQ 上，同一个号不能同时登两个电脑端。

    所以读它的日志尾巴、翻成人话摆到面板上。认不出来的错误码也把原话给出去
    （用户至少能拿去搜）——**什么都不说**是最坏的一种。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write_log(self, text: str) -> None:
        (self.dir / "napcat.log").write_text(text, encoding="utf-8")

    def test_says_what_already_logged_in_means(self):
        # 注意带上颜色转义码 —— 这是 NapCat 真实写出来的格式
        # （没有它的话这条测试是我按"我以为的格式"写的，一拿真日志就废）
        self.write_log(
            "10-13 12:53:36 [info] 请扫描下面的二维码，然后在手Q上授权登录：\n"
            "10-13 12:53:44 [\x1b[31merror\x1b[39m] 当前账号(123456789)已登录,无法重复登录\n")
        hint = deploy.napcat_error_hint(self.dir)
        self.assertIn("电脑上那个 QQ", hint)
        self.assertIn("换一个号", hint)

    def test_unknown_error_gives_the_raw_line(self):
        self.write_log("10-13 12:00:00 [error] 某个没见过的问题\n")
        self.assertIn("某个没见过的问题", deploy.napcat_error_hint(self.dir))

    def test_no_error_no_noise(self):
        self.write_log("10-13 12:00:00 [info] 一切正常\n")
        self.assertEqual(deploy.napcat_error_hint(self.dir), "")
        self.assertEqual(deploy.napcat_error_hint(self.dir / "没有这个目录"), "")
