"""第 3 步门禁：企微群机器人推送。

全部离线 —— 不联网、不发真消息。真发消息靠 `python run.py --push` 手动验一次。

这里盯住的是"用户会立刻撞上的那几件事"：
  · 地址复制不全能不能认出来
  · 超长内容会不会被劈成半个汉字
  · 一天 20 条的上限会不会把用户搞崩
  · 发失败了有没有人话解释
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.interfaces import Sink, SinkResult          # noqa: E402
from app.contracts.models import KIND_IMAGE, Attachment, PushPayload  # noqa: E402
from app.sinks.wecom_bot import (                              # noqa: E402
    MARKDOWN_LIMIT, TEXT_LIMIT, WeComBotSink, _multipart,
    fit_bytes, split_for_send, split_webhook,
)


class FakeClock:
    """假时钟：让限速测试不用真的等 60 秒。"""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.slept += seconds


class TestWebhookParsing(unittest.TestCase):
    """用户复制地址时出的岔子，都得认。"""

    # 故意用假的。⚠️ 测试里永远不许用真密钥 ——
    # 源码要开源，放真密钥等于把群机器人的发消息权限交给所有人。
    KEY = "00000000-1111-2222-3333-444444444444"

    def test_full_url(self):
        self.assertEqual(
            split_webhook(f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={self.KEY}"),
            self.KEY)

    def test_missing_question_mark(self):
        """实测真的会漏掉问号：.../sendkey=xxx"""
        self.assertEqual(split_webhook(f"https://x/sendkey={self.KEY}"), self.KEY)

    def test_bare_key(self):
        self.assertEqual(split_webhook(self.KEY), self.KEY)

    def test_extra_params_stripped(self):
        self.assertEqual(split_webhook(f"https://x/send?key={self.KEY}&a=1"), self.KEY)

    def test_garbage_is_rejected(self):
        """一段中文提示语不该被当成 key。"""
        for text in ("", "   ", "这里粘贴 Webhook 地址", "https://qyapi.weixin.qq.com/"):
            with self.subTest(text=text):
                self.assertEqual(split_webhook(text), "")


class TestByteTruncation(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(fit_bytes("你好", 100), "你好")

    def test_truncation_does_not_split_chinese(self):
        """按字节切容易把一个汉字劈成半个 —— 那会变成乱码或直接报错。"""
        text = "交作业" * 500
        result = fit_bytes(text, 100)
        self.assertLessEqual(len(result.encode("utf-8")), 100)
        self.assertTrue(result.endswith("…"))
        result.encode("utf-8").decode("utf-8")      # 能解码就说明没劈坏

    def test_exact_limit_untouched(self):
        text = "a" * 50
        self.assertEqual(fit_bytes(text, 50), text)


class TestSplitForSend(unittest.TestCase):
    """一天的汇总装不下 4096 字节是常态，必须会切。"""

    def test_short_content_single_chunk(self):
        self.assertEqual(split_for_send("一行\n两行", MARKDOWN_LIMIT), ["一行\n两行"])

    def test_long_content_splits_on_line_boundaries(self):
        lines = [f"第 {i} 条待办：明天下午三点交作业" for i in range(400)]
        chunks = split_for_send("\n".join(lines), MARKDOWN_LIMIT)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.encode("utf-8")), MARKDOWN_LIMIT)
        # 切完之后内容不能丢：拼起来行数要对得上
        rebuilt = "\n".join(chunks).splitlines()
        self.assertEqual(len(rebuilt), len(lines))

    def test_single_giant_line_still_splits(self):
        chunks = split_for_send("字" * 3000, TEXT_LIMIT)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.encode("utf-8")), TEXT_LIMIT)


class TestMultipart(unittest.TestCase):
    def test_body_carries_file_bytes_and_boundary(self):
        body, content_type = _multipart("media", "作业.pdf", b"HELLO")
        self.assertIn("boundary=", content_type)
        self.assertIn(b"HELLO", body)
        self.assertIn(b"file", body)
        self.assertIn("作业.pdf".encode("utf-8"), body)

    def test_upload_url_carries_type_not_the_form(self):
        """回归：type=file 必须走 URL 查询串。

        放错地方官方会回 40058 missing param 'type'，
        报错文字却像在说"消息格式不对"，极容易查错方向。
        """
        captured: dict[str, str] = {}
        sink = WeComBotSink(webhook="abc-key")
        tmp = Path(__file__).parent / "_tmp_upload.txt"
        tmp.write_text("hello", encoding="utf-8")

        def fake_post(url: str, body: bytes, content_type: str) -> dict:
            captured["url"] = url
            captured["body"] = body.decode("utf-8", "ignore")
            return {"errcode": 0, "media_id": "FAKE_MEDIA_ID"}

        sink._post_form = fake_post                 # type: ignore[method-assign]
        sink._post_json = lambda _p: {"errcode": 0}  # type: ignore[method-assign]
        try:
            result = sink.send_file(str(tmp))
        finally:
            tmp.unlink(missing_ok=True)

        self.assertTrue(result.ok, result.message)
        self.assertIn("type=file", captured["url"])
        self.assertIn("key=abc-key", captured["url"])
        self.assertNotIn('name="type"', captured["body"])


class TestReadiness(unittest.TestCase):
    def test_not_ready_without_webhook(self):
        sink = WeComBotSink(webhook="", enabled=True)
        self.assertFalse(sink.ready())
        self.assertIn("还没填", sink.describe())

    def test_not_ready_when_disabled(self):
        sink = WeComBotSink(webhook="abc-key", enabled=False)
        self.assertFalse(sink.ready())
        self.assertIn("未启用", sink.describe())

    def test_describe_hides_most_of_the_key(self):
        """日志和截图里不能把完整密钥漏出去。"""
        sink = WeComBotSink(webhook="abcdef123456", enabled=True)
        self.assertIn("…3456", sink.describe())
        self.assertNotIn("abcdef123456", sink.describe())
        self.assertNotIn("123456", sink.describe())

    def test_short_key_is_fully_masked(self):
        """太短的 key 露 4 位等于露一半，干脆整个盖掉。"""
        sink = WeComBotSink(webhook="abcd1234", enabled=True)
        self.assertIn("☓☓☓☓", sink.describe())
        self.assertNotIn("abcd1234", sink.describe())

    def test_implements_sink_interface(self):
        self.assertIsInstance(WeComBotSink(), Sink)

    def test_deliver_refuses_when_not_configured(self):
        result = WeComBotSink(webhook="").deliver(PushPayload(channel="wecom_bot", body="hi"))
        self.assertFalse(result.ok)
        self.assertIn("Webhook", result.message)


class TestThrottle(unittest.TestCase):
    """官方限制每分钟 20 条，撞上就丢消息，所以本地必须先排队。"""

    def test_allows_up_to_limit_without_waiting(self):
        clock = FakeClock()
        sink = WeComBotSink(webhook="k", max_per_minute=3,
                            now=clock.now, sleep=clock.sleep)
        for _ in range(3):
            sink._throttle()
        self.assertEqual(clock.slept, 0.0)

    def test_waits_when_limit_reached(self):
        clock = FakeClock()
        sink = WeComBotSink(webhook="k", max_per_minute=2, window_seconds=60,
                            now=clock.now, sleep=clock.sleep)
        sink._throttle()
        sink._throttle()
        sink._throttle()          # 第 3 条：必须等到窗口滑过去
        self.assertGreaterEqual(clock.slept, 60.0)


class TestErrorHandling(unittest.TestCase):
    def _sink_with_response(self, payload: dict) -> WeComBotSink:
        sink = WeComBotSink(webhook="abc-key", retry_times=0,
                            now=FakeClock().now, sleep=lambda _s: None)
        sink._post_json = lambda _p: payload      # type: ignore[method-assign]
        return sink

    def test_success(self):
        sink = self._sink_with_response({"errcode": 0, "errmsg": "ok"})
        self.assertTrue(sink.send_text("你好").ok)

    def test_bad_webhook_translated_to_human(self):
        sink = self._sink_with_response({"errcode": 93000, "errmsg": "invalid webhook url"})
        result = sink.send_text("你好")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, 93000)
        self.assertIn("Webhook 地址无效", sink.explain(result))

    def test_unknown_error_code_still_readable(self):
        sink = self._sink_with_response({"errcode": 12345, "errmsg": "some new error"})
        text = sink.explain(sink.send_text("你好"))
        self.assertIn("12345", text)
        self.assertIn("some new error", text)

    def test_network_failure_does_not_raise(self):
        """网络断了必须是返回失败，不能抛异常把主程序带崩。"""
        sink = WeComBotSink(webhook="abc-key", retry_times=0,
                            now=FakeClock().now, sleep=lambda _s: None)

        def boom(_payload):
            raise OSError("network is unreachable")

        sink._post_json = boom                     # type: ignore[method-assign]
        result = sink.send_text("你好")
        self.assertFalse(result.ok)
        self.assertIn("network is unreachable", result.message)

    def test_empty_content_not_sent(self):
        self.assertFalse(WeComBotSink(webhook="abc-key").send_text("   ").ok)


class TestAttachments(unittest.TestCase):
    def test_missing_attachment_does_not_break_body(self):
        """附件没了不能让正文也发不出去。"""
        sink = WeComBotSink(webhook="abc-key", retry_times=0,
                            now=FakeClock().now, sleep=lambda _s: None)
        sent: list[str] = []
        sink._post_json = lambda p: (sent.append(p["msgtype"]) or {"errcode": 0})  # type: ignore[method-assign]
        payload = PushPayload(channel="wecom_bot", body="今天要做的",
                              attachments=[Attachment(name="没了.jpg", path="Z:/不存在.jpg",
                                                      kind=KIND_IMAGE)])
        result = sink.deliver(payload)
        self.assertTrue(result.ok)                 # 正文成功
        self.assertIn("没发成", result.message)     # 但告诉用户附件没发出去
        self.assertEqual(sent, ["text"])

    def test_image_over_2mb_reported(self):
        sink = WeComBotSink(webhook="abc-key")
        big = Path(__file__).parent / "_tmp_big.jpg"
        big.write_bytes(b"x" * 2_100_000)
        try:
            result = sink.send_image(str(big))
            self.assertFalse(result.ok)
            self.assertIn("太大", result.message)
        finally:
            big.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
