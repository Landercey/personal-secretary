"""第 4 步门禁（三）：真的开一个端口，真的连上去。

前面两个测试文件只验"给一条事件，能不能解析对"。这个文件验的是更外面一层：
端口能不能绑上、握手会不会被拒、一条 JSON 过来能不能走到回调。

这一层最容易出的问题是"代码看着没错，但就是收不到消息"，
所以必须真的连一次 —— 全部在本机 127.0.0.1 上，不联网、不碰 QQ。
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.sources.qq import QQSettings, QQSource                  # noqa: E402

try:
    import websockets
    from websockets.asyncio.client import connect
    HAS_WEBSOCKETS = True
except ImportError:                                             # pragma: no cover
    HAS_WEBSOCKETS = False

TOKEN = "test-token-123456"
PATH = "/onebot/v11/ws"


def free_port() -> int:
    """向系统要一个没人用的端口。

    写死端口的话，测试并行跑或者上一次没退干净就会撞车。
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until(predicate, timeout: float = 8.0, interval: float = 0.05) -> bool:
    """等某个条件成立。开服务是异步的，测试得等它起来。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@unittest.skipUnless(HAS_WEBSOCKETS, "没装 websockets，跳过真实连接测试")
class TestQQServerIntegration(unittest.TestCase):
    """起真服务、连真端口。"""

    def setUp(self):
        self.port = free_port()
        self.received: list = []
        self.source = QQSource(
            settings=QQSettings(
                enabled=True,
                bind_host="127.0.0.1",
                bind_port=self.port,
                path=PATH,
                token=TOKEN,
                group_ids=[555666],
            ),
            on_message=self.received.append,
        )
        self.assertTrue(self.source.start(), "服务没能启动")
        self.assertTrue(
            wait_until(lambda: self.source.status().get("running")),
            f"端口没监听上：{self.source.status()}")

    def tearDown(self):
        self.source.stop()

    # ------------------------------------------------------------------ 工具
    def url(self, token: str = TOKEN, path: str = PATH) -> str:
        base = f"ws://127.0.0.1:{self.port}{path}"
        return f"{base}?access_token={token}" if token else base

    def event(self, text: str, message_id: int = 1) -> str:
        return json.dumps({
            "post_type": "message",
            "message_type": "group",
            "self_id": "10001",
            "group_id": 555666,
            "user_id": 12345,
            "message_id": message_id,
            "time": 1770000000,
            "message": [{"type": "text", "data": {"text": text}}],
            "sender": {"user_id": 12345, "card": "张三"},
        }, ensure_ascii=False)

    def run_client(self, coro_factory) -> None:
        asyncio.run(coro_factory())

    # ------------------------------------------------------------------ 正常
    def test_message_flows_from_socket_to_callback(self):
        """最核心的一条：NapCat 那样的客户端连上来发一条，回调要收到。"""

        async def scenario():
            async with connect(self.url()) as ws:
                await ws.send(self.event("明天下午三点交作业"))
                # 等回调真的被调用（网络是异步的，不能马上断言）
                await asyncio.sleep(0.3)

        self.run_client(scenario)
        self.assertTrue(wait_until(lambda: len(self.received) >= 1),
                        "回调没收到消息")
        self.assertIn("交作业", self.received[0].text)
        self.assertEqual(self.source.status()["accepted"], 1)

    def test_meta_event_records_self_id(self):
        """NapCat 的心跳里带着登录的 QQ 号，要记下来。"""

        async def scenario():
            async with connect(self.url()) as ws:
                await ws.send(json.dumps({"post_type": "meta_event", "self_id": "10001"}))
                await asyncio.sleep(0.3)

        self.run_client(scenario)
        self.assertTrue(wait_until(lambda: self.source.status().get("self_id") == "10001"))

    def test_notice_events_are_ignored(self):
        """戳一戳、撤回这类不是消息，不该进待办。"""

        async def scenario():
            async with connect(self.url()) as ws:
                await ws.send(json.dumps({"post_type": "notice", "notice_type": "poke"}))
                await asyncio.sleep(0.3)

        self.run_client(scenario)
        self.assertEqual(self.received, [])

    def test_garbage_frame_does_not_kill_the_connection(self):
        """收到不是 JSON 的东西，跳过就好，连接不能断。"""

        async def scenario():
            async with connect(self.url()) as ws:
                await ws.send("这不是 JSON")
                await ws.send("[1,2,3]")
                await ws.send(self.event("明天交作业", message_id=2))
                await asyncio.sleep(0.3)

        self.run_client(scenario)
        self.assertTrue(wait_until(lambda: len(self.received) >= 1))
        self.assertIn("交作业", self.received[0].text)

    # ------------------------------------------------------------------ 拒绝
    def test_wrong_path_is_rejected(self):
        """路径不对 → 拒。防止别的程序误连上来。"""

        async def scenario():
            with self.assertRaises(Exception):
                async with connect(self.url(path="/wrong/path")):
                    await asyncio.sleep(0.2)

        self.run_client(scenario)

    def test_wrong_token_is_rejected(self):
        """令牌不对 → 拒。"""

        async def scenario():
            with self.assertRaises(Exception):
                async with connect(self.url(token="bad-token")):
                    await asyncio.sleep(0.2)

        self.run_client(scenario)

    def test_token_via_authorization_header_also_works(self):
        """有的客户端把令牌放请求头，不放地址里 —— 两种都要认。"""

        async def scenario():
            async with connect(self.url(token=""),
                               additional_headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
                await ws.send(self.event("明天交作业"))
                await asyncio.sleep(0.3)

        self.run_client(scenario)
        self.assertTrue(wait_until(lambda: len(self.received) >= 1))

    def test_empty_token_disables_the_check(self):
        """没配令牌时不校验（本地自用可以接受，文档会建议配一个）。"""
        port = free_port()
        source = QQSource(settings=QQSettings(
            enabled=True, bind_host="127.0.0.1", bind_port=port,
            path=PATH, token="", group_ids=[555666]))
        source.start()
        try:
            self.assertTrue(wait_until(lambda: source.status().get("running")))

            async def scenario():
                async with connect(f"ws://127.0.0.1:{port}{PATH}") as ws:
                    await ws.send(self.event("明天交作业"))
                    await asyncio.sleep(0.3)

            asyncio.run(scenario())
        finally:
            source.stop()


@unittest.skipUnless(HAS_WEBSOCKETS, "没装 websockets，跳过真实连接测试")
class TestPortConflict(unittest.TestCase):
    """端口被别人占着时，程序不能崩，要能给出人话提示并自己重试。"""

    def test_busy_port_reports_friendly_error(self):
        port = free_port()
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        try:
            source = QQSource(settings=QQSettings(
                enabled=True, bind_host="127.0.0.1", bind_port=port, path=PATH))
            source.start()
            self.assertTrue(wait_until(lambda: "占用" in source.status().get("error", "")),
                            f"没给出端口占用提示：{source.status()}")
            self.assertFalse(source.status().get("running"))
            source.stop()
        finally:
            blocker.close()


if __name__ == "__main__":
    unittest.main()
