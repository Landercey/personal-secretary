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

    def test_fuzzy_day_alone_is_chat(self):
        """只说了"今天/明天"、没说要做什么 → 是聊天。

        2026-10-13 用户的待办列表里 8 条有 4 条是这么进来的：
        "明天要下雨，哦耶耶耶"、"今天我拍了同学一张照片…"、"导致明天下雨"、
        "快下令让明天天晴"。光凭一个"明天"就收，列表会被聊天刷满。
        """
        for text in ("明天要下雨，哦耶耶耶",
                     "今天我拍了同学一张照片，结果不知道为什么突然没有了",
                     "导致明天下雨",
                     "[回复]@某人 将军！ 快下令让明天天晴"):
            with self.subTest(text=text):
                ok, why = is_actionable(text)
                self.assertFalse(ok, f"这是聊天，不该收：{text}（{why}）")

    def test_concrete_time_alone_is_still_a_todo(self):
        """"10月30号 18:30 交作业"没动词，但那明显是正事 —— 这条不能一起收紧掉。"""
        for text in ("10月30号 18:30 交作业",
                     "10 月 14 号教学楼 302 下午 1 点半交表，带身份证",
                     "周三下午 14:00 在 6C205 上大英课"):
            with self.subTest(text=text):
                ok, why = is_actionable(text)
                self.assertTrue(ok, f"有具体时间的就是正事：{text}（{why}）")

    def test_at_all_is_not_stripped_before_the_strong_check(self):
        """@全体 是"这是一条通知"的信号，不能因为去占位符把它一起去掉。

        真踩过：strip_placeholders 会把 "@全体" 去掉，然后在去完的那份上判
        "有没有强通知标记" —— 永远判不到，于是"@全体 明天没有晚自习"被丢掉。
        """
        ok, why = is_actionable("@全体 明天没有晚自习")
        self.assertTrue(ok, why)

    def test_loose_keeps_the_fuzzy_day_chats(self):
        """嫌丢太多就把过滤强度改成 loose —— 那句承诺得真的管用。"""
        from app.runtime.config import config

        original = config["parse"]["filter_strength"]
        try:
            config["parse"]["filter_strength"] = "loose"
            self.assertTrue(is_actionable("明天要下雨，哦耶耶耶")[0])
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


class TestFileTextTitles(unittest.TestCase):
    """截图/附件里读出来的字，切出来的标题要再按"这句话是不是一件事"过一遍。

    2026-10-17 查真实数据：8 条误收里有 4 条是截图 OCR 的纯名词碎片 ——
    「成华区」（天气小程序）、「Q 成都奶龙大学 ℃ om」（招新海报边角）、
    「0四川省高校心理健康教育专委会」（讲座海报抬头）。
    它们能混进来是因为"要不要收"那把尺子量的是**整条消息**（时间还在原文里），
    而切出来的标题只剩一个名字。
    """

    @staticmethod
    def _run(text, explicit=False, inbox=False):
        from app.pipeline.chain import Pipeline
        from app.pipeline.parse_enricher import RuleParseEnricher

        pipeline = Pipeline(draft_enrichers=[ChatNoiseEnricher(), RuleParseEnricher()])
        message = Message(
            msg_id="qq:9",
            source_kind=SOURCE_QQ_INBOX if inbox else SOURCE_QQ_GROUP,
            source_name="测试群", text=text,
            raw={"text_from_file": True, "explicit": explicit})
        return pipeline.run(message)[1]

    def test_screenshot_garbage_does_not_become_a_todo(self):
        for text in ("成华区", "Q 成都奶龙大学 ℃ om",
                     "0四川省高校心理健康教育专委会"):
            with self.subTest(text=text):
                self.assertEqual(self._run(text), [])

    def test_a_real_notice_from_a_poster_survives(self):
        drafts = self._run("ICT双选会志愿者招募\n一、招募人数 5人\n二、活动时间 10月14日14：00")
        self.assertEqual(len(drafts), 1)
        self.assertIn("志愿者招募", drafts[0].title)

    def test_the_user_asked_for_it_so_no_judging(self):
        """界面上点着「变成待办」拖进来的，不许再替他判断。"""
        drafts = self._run("成华区", explicit=True)
        self.assertEqual(len(drafts), 1)

    def test_the_inbox_is_never_filtered(self):
        """小号收件箱全盘接收 —— 发一张图过去就是明说"这个我要记下来"。"""
        drafts = self._run("成华区", inbox=True)
        self.assertEqual(len(drafts), 1)


if __name__ == "__main__":
    unittest.main()
