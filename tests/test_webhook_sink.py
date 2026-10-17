"""通用推送地址（钉钉 / 飞书 / Bark，另有一个只做兼容的旧服务）。

盯住这类通道最容易出错的三件事：
  · 认错地址 → 报文形状不对 → 对方回 400 或者干脆忽略
  · **对方失败时也回 HTTP 200** → 不看 body 里的错误码就会"假装发成功"
  · 地址里带着 token → 一旦写进日志或反馈包，等于把发消息的权限交出去
"""

from __future__ import annotations

import importlib
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import Attachment, PushPayload          # noqa: E402
from app.sinks.webhook import (                                   # noqa: E402
    DINGTALK_KEYWORD, WebhookSink, _verdict, vendor_of,
)
from app.runtime.config import Config                             # noqa: E402
from app.ui.bridge import Api                                     # noqa: E402

# 必须按模块路径取：app/runtime/__init__.py 里 `config` 那个名字被全局实例占了
config_module = importlib.import_module("app.runtime.config")

# 故意用假的。测试里永远不许用真 token —— 源码要开源。
FAKE = "00000000-1111-2222-3333-444444444444"

RECORDED: list[dict] = []


class Recorder(BaseHTTPRequestHandler):
    """假的目标服务：把收到的请求记下来，按 `reply` 回。"""

    reply = (200, b'{"errcode":0}')

    def do_POST(self):                                            # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        RECORDED.append({"path": self.path,
                         "body": self.rfile.read(length).decode("utf-8", "replace")})
        status, payload = type(self).reply
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):                                # 别弄脏测试输出
        pass


class NoSleep:
    """假 sleep：重试测试不用真的等。"""

    def __init__(self) -> None:
        self.total = 0.0

    def __call__(self, seconds: float) -> None:
        self.total += seconds


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        cls.port = cls.server.server_address[1]
        cls.url = f"http://127.0.0.1:{cls.port}/hook"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        RECORDED.clear()
        Recorder.reply = (200, b'{"errcode":0}')
        self.sleep = NoSleep()

    def sink(self, **kwargs) -> WebhookSink:
        kwargs.setdefault("url", self.url)
        kwargs.setdefault("sleep", self.sleep)
        return WebhookSink(**kwargs)


class TestVendorDetection(unittest.TestCase):
    def test_known_hosts(self):
        cases = {
            # 企业微信的地址填进通用地址也得认 —— 用户手里先拿到的往往是它
            f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={FAKE}": "企业微信机器人",
            "https://oapi.dingtalk.com/robot/send?access_token=x": "钉钉机器人",
            "https://open.feishu.cn/open-apis/bot/v2/hook/abc": "飞书机器人",
            # 这家后来收费了，不再推荐；识别能力留着只是为了不让老配置直接失效
            f"https://www.pushplus.plus/send?token={FAKE}": "PushPlus（收费，不再推荐）",
            f"https://api.day.app/{FAKE}": "Bark",
            f"http://127.0.0.1:8000/hook?token={FAKE}": "自定义地址",
        }
        for url, label in cases.items():
            with self.subTest(url=url):
                self.assertEqual(vendor_of(url)[0], label)

    def test_shapes_match_the_docs(self):
        """每家的报文形状：照官方文档写，改这里之前先回文档看一眼。"""
        build = vendor_of("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x")[1]
        payload, field, good = build("标题", "正文")
        self.assertEqual(payload["msgtype"], "text")
        self.assertIn("正文", payload["text"]["content"])
        self.assertEqual((field, good), ("errcode", ("0",)))

        build = vendor_of("https://oapi.dingtalk.com/robot/send?access_token=x")[1]
        payload, field, good = build("标题", "正文")
        # 钉钉的「自定义关键词」安全设置要求报文里出现那个词，否则直接被拒
        self.assertEqual(payload["msgtype"], "text")
        self.assertIn(DINGTALK_KEYWORD, payload["text"]["content"])
        self.assertEqual((field, good), ("errcode", ("0",)))

        build = vendor_of("https://open.feishu.cn/open-apis/bot/v2/hook/x")[1]
        payload, field, good = build("标题", "正文")
        self.assertEqual(payload["msg_type"], "text")
        self.assertIn("正文", payload["content"]["text"])
        self.assertEqual((field, good), ("code", ("0",)))

        build = vendor_of("https://www.pushplus.plus/send?token=x")[1]
        payload, field, good = build("标题", "正文")
        # 必须用 txt：默认的 html 模板会把换行吃掉
        self.assertEqual(payload["template"], "txt")
        self.assertEqual((field, good), ("code", ("200",)))

        build = vendor_of("https://api.day.app/x")[1]
        payload, field, good = build("标题", "正文")
        self.assertEqual(payload["body"], "正文")
        self.assertEqual((field, good), ("code", ("200",)))


class TestVerdict(unittest.TestCase):
    """**失败时也回 HTTP 200** —— 这一层就是为它存在的。"""

    def test_dingtalk_ok_and_error(self):
        self.assertTrue(_verdict('{"errcode":0,"errmsg":"ok"}', "errcode", ("0",))[0])
        ok, code, message = _verdict(
            '{"errcode":310000,"errmsg":"keywords not in content"}', "errcode", ("0",))
        self.assertFalse(ok)
        self.assertEqual(code, 310000)
        self.assertIn("keywords", message)        # 原文带上，用户搜得到

    def test_pushplus_and_feishu(self):
        self.assertTrue(_verdict('{"code":200,"msg":"请求成功"}', "code", ("200",))[0])
        self.assertFalse(_verdict('{"code":500,"msg":"token无效"}', "code", ("200",))[0])
        self.assertTrue(_verdict('{"code":0,"msg":"success"}', "code", ("0",))[0])

    def test_non_json_body(self):
        """勾了错误码字段的通道，收到一坨非 JSON 的东西不算成功 —— 不许猜。"""
        self.assertFalse(_verdict("<html>404</html>", "errcode", ("0",))[0])
        # 认不出的自定义地址没得判，HTTP 200 就算发到了
        self.assertTrue(_verdict("ok", "", ())[0])


class TestSink(Base):
    def test_describe_never_leaks_the_token(self):
        sink = self.sink(url=f"https://www.pushplus.plus/send?token={FAKE}")
        text = sink.describe()
        self.assertNotIn(FAKE, text)
        self.assertIn("PushPlus", text)

    def test_not_ready_when_disabled_or_empty(self):
        self.assertFalse(self.sink(url="", enabled=True).ready())
        self.assertFalse(self.sink(url=self.url, enabled=False).ready())
        payload = PushPayload(channel="webhook", body="hi")
        self.assertFalse(self.sink(url="", enabled=True).deliver(payload).ok)

    def test_delivers_over_real_http(self):
        result = self.sink().deliver(
            PushPayload(channel="webhook", title="早上好", body="1. 拿快递"))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(len(RECORDED), 1)
        self.assertIn("拿快递", RECORDED[0]["body"])

    def test_vendor_failure_is_reported_not_swallowed(self):
        """对方回 200 但 body 说失败 —— 必须当成失败。"""
        payload = {"msgtype": "text"}
        self.assertTrue(self.sink()._post(payload, "errcode", ("0",)).ok)
        Recorder.reply = (200, b'{"errcode":310000,"errmsg":"keywords not in content"}')
        result = self.sink()._post(payload, "errcode", ("0",))
        self.assertFalse(result.ok)
        self.assertEqual(result.code, 310000)

    def test_http_error_retries_then_gives_up(self):
        Recorder.reply = (500, b'{"errcode":0}')
        result = self.sink(retry_times=2).deliver(PushPayload(channel="webhook", body="hi"))
        self.assertFalse(result.ok)
        self.assertIn("500", result.message)
        self.assertEqual(len(RECORDED), 3)               # 1 次 + 2 次重试
        self.assertGreater(self.sleep.total, 0)          # 中间确实等了，不是死循环猛打

    def test_http_error_keeps_the_reason_from_the_body(self):
        """4xx 时对方会说明原因 —— 只报状态码用户查不动。"""
        Recorder.reply = (400, '{"message":"device key not found"}'.encode())
        result = self.sink(retry_times=0).deliver(
            PushPayload(channel="webhook", body="hi"))
        self.assertFalse(result.ok)
        self.assertIn("400", result.message)
        self.assertIn("device key not found", result.message)

    def test_attachments_are_reported_as_not_sent(self):
        """这几家都没有发文件的接口 —— 得明说，不能让用户以为附件也过去了。"""
        item = Attachment(name="作业.docx", path=str(ROOT / "README.md"))
        result = self.sink().deliver(
            PushPayload(channel="webhook", body="正文", attachments=[item]))
        self.assertTrue(result.ok)
        self.assertIn("附件没发", result.message)
        self.assertIn("作业.docx", result.message)
        self.assertEqual(len(RECORDED), 1)               # 附件没走网络

    def test_test_push_button_goes_through_the_real_chain(self):
        """界面上的「发一条测试推送」走的是和定时推送**同一条路**。

        直接用假的目标服务当通道：点一下应该真的发出去一条、
        而且内容里看得出是测试。跑不通的话这个按钮就是摆设。
        """
        original = config_module.config
        config_module.config = Config({
            "push_webhook": {"enabled": True, "url": self.url, "retry_times": 0},
        })
        self.addCleanup(setattr, config_module, "config", original)

        result = Api(None).test_push()
        self.assertTrue(result["ok"], result["message"])
        self.assertEqual(result["channel"], "webhook")
        self.assertEqual(len(RECORDED), 1)
        self.assertIn("测试推送", RECORDED[0]["body"])
