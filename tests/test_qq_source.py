"""第 4 步门禁（二）：QQ 事件的解析与接收范围。

全部离线 —— 不开端口、不登 QQ、不联网。
`QQSource.feed_event()` 不碰网络，所以可以直接喂假事件进来。

这里盯住的是"用户会觉得莫名其妙"的那几件事：
  · 自己发给自己小号的消息，为什么没收？
  · 没勾的群，为什么没收？
  · 一条正常的群通知，为什么标题变成了 [CQ:xxxx]？
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import SOURCE_QQ_GROUP, SOURCE_QQ_INBOX      # noqa: E402
from app.sources.qq import (                                          # noqa: E402
    KIND_GROUP, KIND_PRIVATE, QQSettings, QQSource,
    chat_identity, ensure_token, event_to_message, flatten_message,
    self_message_target, today_allday,
)

SELF = "10001"
GROUP = 555666
FRIEND = 999888


def group_event(text: str, group_id: int = GROUP, sender: str = "张三",
                self_id: str = SELF, at_self: bool = False) -> dict:
    """造一条别人在群里发的消息事件。"""
    message = [{"type": "text", "data": {"text": text}}]
    if at_self:
        message.insert(0, {"type": "at", "data": {"qq": self_id}})
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": self_id,
        "group_id": group_id,
        "user_id": 12345,
        "message_id": 101,
        "time": 1770000000,
        "message": message,
        "sender": {"user_id": 12345, "card": sender, "nickname": sender},
    }


def sent_event(target: str, text: str, self_id: str = SELF) -> dict:
    """造一条"我自己发出去"的消息事件。

    注意 post_type 是 message_sent（不是 message）——
    旧项目一开始只认 message，导致"发给自己小号"全被扔掉。
    """
    return {
        "post_type": "message_sent",
        "message_type": "private",
        "self_id": self_id,
        "user_id": int(self_id),
        "target_id": int(target),
        "message_id": 202,
        "time": 1770000001,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": int(self_id), "nickname": "我"},
    }


class TestFlattenMessage(unittest.TestCase):
    def test_text_segments_joined(self):
        event = {"message": [
            {"type": "text", "data": {"text": "明天"}},
            {"type": "text", "data": {"text": "下午三点交作业"}},
        ]}
        text, mentioned, _images, _voices = flatten_message(event, SELF)
        self.assertEqual(text, "明天下午三点交作业")
        self.assertFalse(mentioned)

    def test_at_self_is_marked(self):
        event = {"message": [
            {"type": "at", "data": {"qq": SELF}},
            {"type": "text", "data": {"text": "开会"}},
        ]}
        text, mentioned, _i, _v = flatten_message(event, SELF)
        self.assertTrue(mentioned)
        self.assertIn("@我", text)

    def test_at_all_and_other(self):
        event = {"message": [
            {"type": "at", "data": {"qq": "all"}},
            {"type": "at", "data": {"qq": "777"}},
        ]}
        text, mentioned, _i, _v = flatten_message(event, SELF)
        self.assertIn("@全体", text)
        self.assertIn("@某人", text)
        self.assertFalse(mentioned)

    def test_at_all_does_not_leave_a_stray_member(self):
        """QQ 把"@全体成员"拆成 @段 + 文字"成员"两截，别让标题尾巴挂个孤零零的"成员"。

        2026-10-16 用户报的：#40 标题末尾"…参加军训！成员"。
        """
        event = {"message": [
            {"type": "text", "data": {"text": "明天记得穿军训服"}},
            {"type": "at", "data": {"qq": "all"}},
            {"type": "text", "data": {"text": "成员"}}]}
        text, *_ = flatten_message(event, SELF)
        self.assertIn("@全体", text)
        self.assertNotIn("成员", text.replace("@全体", ""))

    def test_media_becomes_placeholder(self):
        event = {"message": [
            {"type": "image", "data": {"url": "http://x/a.jpg"}},
            {"type": "face", "data": {}},
            {"type": "record", "data": {"url": "http://x/a.silk"}},
        ]}
        text, _m, images, voices = flatten_message(event, SELF)
        self.assertIn("[图片]", text)
        self.assertIn("[表情]", text)
        self.assertIn("[语音]", text)
        self.assertEqual(images, ["http://x/a.jpg"])
        self.assertEqual(voices, ["http://x/a.silk"])

    def test_cq_code_string_falls_back_to_clean_text(self):
        """有些实现只给 raw_message 的 CQ 码串。

        不清理的话标题会变成一串看不懂的代码 —— 旧库里真出现过这种待办。
        """
        event = {"message": "", "raw_message": "[CQ:at,qq=123] 明天交作业"}
        text, _m, _i, _v = flatten_message(event, SELF)
        self.assertEqual(text, "明天交作业")
        self.assertNotIn("CQ:", text)

    def test_flashtransfer_card(self):
        event = {"message": [{"type": "json", "data": {"prompt": "[QQ闪传] 学生守则"}}]}
        text, _m, _i, _v = flatten_message(event, SELF)
        self.assertIn("学生守则", text)

    def test_merged_forward_is_read_out(self):
        """合并转发要把里面的字读出来（2026-10-16）。

        以前一律写成 "[合并转发]"，里面的通知一个字都拿不到 —— 用户库里的废件箱
        堆了几百条 '[合并转发]'，全是这么丢的。NapCat 打开 parseMultMsg 之后
        节点内容会带下来，字段名各版本不一样，所以这里把两种写法都验一遍。
        """
        nodes = {"content": [{"message": [{"type": "text",
                                          "data": {"text": "12月20日前交论文"}}]},
                             {"text": "地点：学习通"}]}
        text, _m, _i, _v = flatten_message({"message": [
            {"type": "forward", "data": nodes}]}, SELF)
        self.assertIn("12月20日前交论文", text)
        self.assertIn("学习通", text)

        other_shape = {"messages": [{"content": [{"type": "text",
                                                 "data": {"text": "明天体检"}}]}]}
        text2, *_ = flatten_message({"message": [
            {"type": "forward", "data": other_shape}]}, SELF)
        self.assertIn("明天体检", text2)

    def test_merged_forward_without_content_stays_a_placeholder(self):
        """读不到内容就老实写 [合并转发] —— 别装懂编一段出来。"""
        text, _m, _i, _v = flatten_message(
            {"message": [{"type": "forward", "data": {"id": "abc"}}]}, SELF)
        self.assertEqual(text, "[合并转发]")

    def test_nested_forward_does_not_recurse_forever(self):
        """套娃的合并转发只展开一层（防递归）。"""
        inner = {"message": [{"type": "forward", "data": {"id": "x"}}]}
        text, *_ = flatten_message({"message": [
            {"type": "forward", "data": {"content": [inner]}}]}, SELF)
        self.assertIn("[合并转发]", text)

class TestChatIdentity(unittest.TestCase):
    def test_group(self):
        self.assertEqual(chat_identity({"message_type": "group", "group_id": GROUP})[0],
                         KIND_GROUP)
        self.assertEqual(chat_identity({"message_type": "group", "group_id": GROUP})[1],
                         str(GROUP))

    def test_private(self):
        kind, ref, _name = chat_identity({"message_type": "private", "user_id": FRIEND})
        self.assertEqual(kind, KIND_PRIVATE)
        self.assertEqual(ref, str(FRIEND))

    def test_unknown(self):
        self.assertEqual(chat_identity({}), ("", "", ""))


class TestSelfMessageTarget(unittest.TestCase):
    def test_other_people_are_none(self):
        event = group_event("你好")
        self.assertIsNone(self_message_target(event, SELF))

    def test_self_sent_private_returns_target(self):
        event = sent_event(FRIEND, "买牛奶")
        self.assertEqual(self_message_target(event, SELF), str(FRIEND))

    def test_self_sent_group_returns_group(self):
        event = {"post_type": "message_sent", "message_type": "group",
                 "self_id": SELF, "user_id": int(SELF), "group_id": GROUP}
        self.assertEqual(self_message_target(event, SELF), str(GROUP))

    def test_no_self_id_means_no_judgement(self):
        self.assertIsNone(self_message_target(sent_event(FRIEND, "x"), ""))


class TestEventToMessage(unittest.TestCase):
    def setUp(self):
        self.settings = QQSettings(
            enabled=True, group_ids=[GROUP], inbox_ids=[FRIEND])

    def test_normal_group_message(self):
        message, reason = event_to_message(
            group_event("明天下午三点在图书馆门口集合"), self.settings, SELF)
        self.assertEqual(reason, "")
        self.assertIsNotNone(message)
        self.assertEqual(message.source_kind, SOURCE_QQ_GROUP)
        self.assertEqual(message.sender, "张三")
        self.assertFalse(message.is_self)
        self.assertTrue(message.msg_id.startswith("qq:"))

    def test_unchecked_group_is_rejected(self):
        """没勾的群一条都不收 —— 这是默认行为，不给用户惊吓。"""
        message, reason = event_to_message(
            group_event("明天交作业", group_id=777), self.settings, SELF)
        self.assertIsNone(message)
        self.assertIn("没有勾选", reason)

    def test_receive_all_accepts_every_group(self):
        settings = QQSettings(enabled=True, receive_all=True, inbox_ids=[FRIEND])
        message, _reason = event_to_message(
            group_event("明天交作业", group_id=777), settings, SELF)
        self.assertIsNotNone(message)

    def test_inbox_message_accepted(self):
        """发给自己小号 → 收件箱，source_kind 是 qq_inbox 且 is_self=True。"""
        message, reason = event_to_message(
            sent_event(FRIEND, "买牛奶"), self.settings, SELF)
        self.assertEqual(reason, "")
        self.assertEqual(message.source_kind, SOURCE_QQ_INBOX)
        self.assertTrue(message.is_self)
        self.assertIn("我发的", message.source_name)

    def test_self_sent_to_unlisted_target_is_rejected(self):
        """自己发给别人（不在收件箱名单里）的不收。

        否则你在群里、跟朋友私聊时随口说的话也会被当成待办。
        """
        message, reason = event_to_message(
            sent_event("123456", "晚上吃啥"), self.settings, SELF)
        self.assertIsNone(message)
        self.assertIn("收件箱", reason)

    def test_only_mention_mode(self):
        settings = QQSettings(enabled=True, group_ids=[GROUP], only_mention=True)
        message, reason = event_to_message(group_event("明天交作业"), settings, SELF)
        self.assertIsNone(message)
        self.assertIn("@我", reason)
        message, _reason = event_to_message(
            group_event("明天交作业", at_self=True), settings, SELF)
        self.assertIsNotNone(message)

    def test_anonymous_is_rejected(self):
        event = group_event("明天交作业")
        event["sub_type"] = "anonymous"
        message, reason = event_to_message(event, self.settings, SELF)
        self.assertIsNone(message)
        self.assertIn("匿名", reason)

    def test_non_message_events_are_ignored(self):
        for post_type in ("notice", "request", "meta_event"):
            with self.subTest(post_type=post_type):
                message, reason = event_to_message({"post_type": post_type}, self.settings)
                self.assertIsNone(message)
                self.assertEqual(reason, "非消息事件")

    def test_long_text_is_truncated(self):
        settings = QQSettings(enabled=True, group_ids=[GROUP], max_text_chars=10)
        message, _reason = event_to_message(group_event("字" * 100), settings, SELF)
        self.assertEqual(len(message.text), 10)

    def test_image_urls_kept_in_raw_for_step5(self):
        """图片下载是第 5 步的事，但地址先记下来，免得要重新解析一遍事件。"""
        event = group_event("明天交作业")
        event["message"].append({"type": "image", "data": {"url": "http://x/a.jpg"}})
        message, _reason = event_to_message(event, self.settings, SELF)
        self.assertEqual(message.raw["image_urls"], ["http://x/a.jpg"])


class TestQQSource(unittest.TestCase):
    """整条源的头尾：事件进 → Message 回调出。"""

    def test_accepted_message_calls_back(self):
        got = []
        source = QQSource(
            settings=QQSettings(enabled=True, group_ids=[GROUP]),
            on_message=got.append)
        source.self_id = SELF
        message, reason = source.feed_event(group_event("明天下午三点交作业"))
        self.assertIsNotNone(message)
        self.assertEqual(reason, "")
        self.assertEqual(len(got), 1)
        self.assertEqual(source.status()["accepted"], 1)

    def test_rejected_event_records_reason(self):
        source = QQSource(settings=QQSettings(enabled=True, group_ids=[]))
        source.self_id = SELF
        message, reason = source.feed_event(group_event("明天交作业"))
        self.assertIsNone(message)
        self.assertEqual(source.status()["skipped"], 1)
        self.assertIn("没有勾选", source.status()["last_reason"])

    def test_callback_exception_does_not_kill_the_source(self):
        """处理一条消息时崩了，不能让整个 QQ 接收停摆。"""
        def boom(_message):
            raise RuntimeError("boom")

        source = QQSource(settings=QQSettings(enabled=True, group_ids=[GROUP]),
                          on_message=boom)
        source.self_id = SELF
        message, reason = source.feed_event(group_event("明天交作业"))
        self.assertIsNotNone(message)
        self.assertIn("处理失败", reason)
        self.assertIn("boom", source.status()["error"])

    def test_disabled_source_does_not_start(self):
        source = QQSource(settings=QQSettings(enabled=False))
        self.assertFalse(source.start())
        self.assertIn("未启用", source.describe())

    def test_describe_is_human_readable(self):
        source = QQSource(settings=QQSettings(enabled=True, group_ids=[GROUP]))
        self.assertIn("未启动", source.describe())


class TestHelpers(unittest.TestCase):
    def test_ensure_token_generates_and_stable(self):
        token = ensure_token("")
        self.assertTrue(len(token) > 20)
        self.assertEqual(ensure_token(token), token)      # 已有的不覆盖

    def test_today_allday_is_end_of_day(self):
        self.assertTrue(today_allday().endswith("23:59:00"))

    def test_settings_summary_readable(self):
        summary = QQSettings(group_ids=[1, 2], inbox_ids=[3]).summary()
        self.assertIn("2 个群", summary)
        self.assertIn("1 个收件箱", summary)

    def test_parse_ids_tolerates_bad_input(self):
        from app.sources.qq import _parse_ids

        self.assertEqual(_parse_ids([123, 456]), [123, 456])
        self.assertEqual(_parse_ids("123,456"), [123, 456])
        self.assertEqual(_parse_ids("123，456"), [123, 456])   # 中文逗号
        self.assertEqual(_parse_ids("abc, -5, 0, 7"), [7])     # 非法片段丢掉
        self.assertEqual(_parse_ids(None), [])


if __name__ == "__main__":
    unittest.main()
