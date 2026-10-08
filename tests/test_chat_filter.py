"""第 4 步门禁（一）：群噪声过滤。

这里的用例大多是从真实群里捞出来的原话，不是编的。
每一条都对应一次实际误判 —— 删用例之前先想清楚当初为什么加。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import SOURCE_QQ_GROUP, SOURCE_QQ_INBOX, Message   # noqa: E402
from app.pipeline.chat_filter import ChatNoiseEnricher, is_actionable        # noqa: E402


class TestIsActionable(unittest.TestCase):
    """该收的必须收：这些是真正的群通知。"""

    SHOULD_KEEP = [
        "各位同学：明天下午3点在图书馆门口集合，务必准时",
        "请于本周五前提交实验报告",
        "@全体 明天上午9点开会",
        "记得带上身份证，10点校门口集合",
        "报名截止到10月15日",
        "接龙：参加志愿活动的同学请回复",
        "周三下午两点答辩，地点另行通知",
    ]

    def test_real_notices_are_kept(self):
        for text in self.SHOULD_KEEP:
            with self.subTest(text=text):
                ok, why = is_actionable(text)
                self.assertTrue(ok, f"真通知被误杀了：{text}（{why}）")

    """该拦的必须拦：这些是闲聊。"""

    SHOULD_DROP = [
        "哈哈哈哈哈",
        "收到",
        "在吗",
        "谢谢老板",
        "今天好累啊，不想写作业",          # 情绪
        "明天考试好慌啊",                  # 情绪 + 时间，最容易误收的一类
        "作业写完了",                      # 完成态
        "这周活动是在哪来着？",            # 提问
        "[图片]",                          # 纯占位符
        "[回复]好的",
        "周五之前",                        # 只有时间，没有事情
        "嗯",
    ]

    def test_real_chat_is_dropped(self):
        for text in self.SHOULD_DROP:
            with self.subTest(text=text):
                ok, why = is_actionable(text)
                self.assertFalse(ok, f"闲聊被误收了：{text}")

    def test_placeholder_is_not_an_action_word(self):
        """"[回复]好的"里的"回复"是占位符，不是行动词。这个坑踩过。"""
        ok, _why = is_actionable("[回复]好的")
        self.assertFalse(ok)

    def test_strong_notice_wins_over_question_mark(self):
        """"记得"这类强通知标记，即使句子像问句也得收。"""
        ok, _why = is_actionable("大家注意，别忘了明天交作业吗？")
        self.assertTrue(ok)


class TestChatNoiseEnricher(unittest.TestCase):
    def _message(self, text: str, source_kind: str = SOURCE_QQ_GROUP,
                 is_self: bool = False) -> Message:
        return Message(msg_id="qq:1", source_kind=source_kind,
                       source_name="测试群", text=text, is_self=is_self)

    def test_group_chat_is_blocked_with_reason(self):
        message = self._message("哈哈哈哈哈")
        drafts = ChatNoiseEnricher().process(message, [])
        self.assertEqual(drafts, [])
        self.assertIn("闲聊", message.raw.get("skip_reason", ""))

    def test_group_notice_passes_through(self):
        message = self._message("明天下午三点在图书馆门口集合")
        drafts = ChatNoiseEnricher().process(message, [])
        self.assertEqual(drafts, [])
        self.assertIsNone(message.raw.get("skip_reason"))

    def test_inbox_is_never_filtered(self):
        """小号收件箱是你专门记给自己的。

        "买牛奶"既没时间也没行动词，但它就是一条待办 ——
        按群里的标准会被当成"既没时间也没行动词"丢掉，所以必须例外。
        """
        for text in ("买牛奶", "拿快递", "记得", "随便记一下"):
            with self.subTest(text=text):
                message = self._message(text, source_kind=SOURCE_QQ_INBOX, is_self=True)
                ChatNoiseEnricher().process(message, [])
                self.assertIsNone(message.raw.get("skip_reason"),
                                  f"收件箱内容被判成闲聊了：{text}")

    def test_manual_entry_is_never_filtered(self):
        """手动敲的也不过滤 —— 你自己敲的，不用机器判断。"""
        message = self._message("买牛奶", source_kind="manual", is_self=True)
        ChatNoiseEnricher().process(message, [])
        self.assertIsNone(message.raw.get("skip_reason"))

    def test_existing_drafts_are_left_alone(self):
        """上游已经产出草稿时，这一步不该把人家抹掉。"""
        from app.contracts.models import TaskDraft

        draft = TaskDraft(title="交作业")
        message = self._message("哈哈哈哈哈")
        self.assertEqual(ChatNoiseEnricher().process(message, [draft]), [draft])

    def test_filter_can_be_switched_off(self):
        """三档总开关拨到 off 时，闲聊也放行（用户说碎片是暗号时用）。"""
        from app.runtime.config import config

        original = config["parse"]["filter_strength"]
        try:
            config["parse"]["filter_strength"] = "off"
            self.assertFalse(ChatNoiseEnricher().enabled())
        finally:
            config["parse"]["filter_strength"] = original


class TestVetoStopsTheChain(unittest.TestCase):
    """回归：过滤器判"闲聊"之后，后面的零件不许再处理这条消息。

    真踩过的坑：过滤器返回空列表表示拒绝，但下一个零件看到空列表
    会以为"还没轮到它"，于是又解析了一遍 —— 结果闲聊、吐槽、碎片
    全都进了待办，过滤器形同虚设。
    "拒绝"和"还没轮到"都是空列表，分不出来，所以必须另有标记。
    """

    def test_chat_noise_wins_over_parser(self):
        from app.pipeline.chain import Pipeline
        from app.pipeline.parse_enricher import RuleParseEnricher

        pipeline = Pipeline(draft_enrichers=[ChatNoiseEnricher(), RuleParseEnricher()])
        message = Message(msg_id="qq:1", source_kind=SOURCE_QQ_GROUP,
                          source_name="测试群", text="明天考试好慌啊")
        prepared, drafts = pipeline.run(message)
        self.assertEqual(drafts, [], "闲聊被后面的零件又解析成待办了")
        self.assertIn("吐槽", prepared.raw.get("skip_reason", ""))

    def test_normal_notice_still_goes_through(self):
        from app.pipeline.chain import Pipeline
        from app.pipeline.parse_enricher import RuleParseEnricher

        pipeline = Pipeline(draft_enrichers=[ChatNoiseEnricher(), RuleParseEnricher()])
        message = Message(msg_id="qq:2", source_kind=SOURCE_QQ_GROUP,
                          source_name="测试群", text="各位同学：明天下午三点在图书馆门口集合")
        _prepared, drafts = pipeline.run(message)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].title, "集合")


if __name__ == "__main__":
    unittest.main()
