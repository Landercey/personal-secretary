"""群噪声过滤（理解层第一个零件，order 5）。

=====================================================================
这个文件在干什么
=====================================================================
群里的消息绝大多数是闲聊，不能条条变待办。这里回答一个问题：
    「这句话值得变成待办吗？」
答"不值得"的，一条都不产生，只记一笔日志（进废件箱）。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  群里闲聊还是被判成待办      → 往 IGNORE_WORDS / EMOTION_WORDS / QUESTION_WORDS 加词
  真通知被误杀了              → 往 ACTION_WORDS / STRONG_NOTICE 加词
  吐槽类误收（"明天考试好慌"）→ 往 EMOTION_WORDS 加词
  已经做完的事被收（"作业写完了"）→ 往 DONE_WORDS 加词
  小号收件箱也开始过滤了      → 看 `_is_inbox()`
  所有这些词的判据             → `is_actionable()`

**改词表是最安全的改法**：只影响判"值不值得"，不影响时间识别。
改完必须跑 python -m unittest discover -s tests -t .

=====================================================================
为什么这个文件在 pipeline 而不是 sources
=====================================================================
它要用时间识别（"只有时间没有事情"那种碎片判断），而时间识别属理解层。
放在 sources 里会让"来源"依赖"理解"，模块边界就破了
（tests/test_contract_integrity.py 会直接报错）。
来源只负责把外面的东西搬进来，判"值不值得"是理解层的活。

=====================================================================
词表来源：下面这些词不是想出来的，是从真实群里一条条捞出来总结的。
每一条都对应一次实际误判。删词之前先想清楚为什么当初加它。
=====================================================================
"""

from __future__ import annotations

from ..contracts.interfaces import DraftEnricher
from ..contracts.models import SOURCE_QQ_INBOX, Message, TaskDraft
from ..runtime.config import config
# 判定规则本身搬到了 actionable.py —— 解析器（按行判）和这里（按整条判）
# 必须用同一把尺子，放两处迟早不一致。
from .actionable import (          # noqa: F401  （下面 re-export，别处还在用）
    ACTION_WORDS,
    DONE_WORDS,
    EMOTION_WORDS,
    IGNORE_WORDS,
    MIN_TEXT_CHARS,
    QUESTION_WORDS,
    STRONG_NOTICE,
    is_actionable,
    strip_placeholders,
)


class ChatNoiseEnricher(DraftEnricher):
    """理解层第一个零件：先把群里的闲聊挡掉。

    跑在切分之前（order 5 < 10）。挡掉的话不产生任何草稿，
    原因写进 message.raw["skip_reason"]，由装配点决定要不要记日志。
    """

    name = "chat-filter"
    order = 5

    def process(self, message: Message, drafts: list[TaskDraft]) -> list[TaskDraft]:
        if drafts:
            return drafts
        if not self._should_filter(message):
            return drafts

        text = message.text or ""
        if len(strip_placeholders(text)) < MIN_TEXT_CHARS:
            message.raw["skip_reason"] = "只有表情或空消息"
            return []
        if any(word in text for word in IGNORE_WORDS):
            message.raw["skip_reason"] = "闲聊内容"
            return []
        ok, why = is_actionable(text)
        if not ok:
            message.raw["skip_reason"] = why
            return []
        return drafts

    @staticmethod
    def _should_filter(message: Message) -> bool:
        """哪些来源要过这道关。

        收件箱（自己发给小号）**不过滤**：那是你专门记给自己的，
        "买牛奶"既没时间也没行动词，但它就是一条待办。
        手动敲的字同理 —— 你自己敲的，不用机器判断。

        例外：**附件里读出来的文字要过滤**。文件里大部分内容是资料而不是待办 ——
        实测一份《实验报告要求.docx》里的"字数不少于 2000 字"、
        一张讲座海报 OCR 出来的"成都理工大学"，都被当成待办收进去了。
        判据：FileEnricher 会打上 text_from_file 标记。
        """
        # 用户在界面上**点着按钮**要求的（拖进来 → 点"变成待办"），不替他判断。
        # 他已经明确表达过意图了，再拿规则去猜是跟用户对着干。
        if message.raw.get("explicit"):
            return False
        # 小号收件箱：你专门发给自己的东西，**全盘接收** —— 连文件里的字也不过滤。
        # 这条必须排在 text_from_file 前面：把小号当收件箱的人，
        # 发一份文件过去就是明说"这个我要记下来"，
        # 再拿"文件里大多是资料"挡掉他，等于这个功能白做。
        if message.source_kind == SOURCE_QQ_INBOX:
            return False
        # 别人发来的附件里读出来的文字要过滤：文件里大部分是资料不是待办。
        # （实测那份《实验报告要求.docx》的"字数不少于 2000 字"、
        #   讲座海报 OCR 出来的"成都理工大学"都是这么漏进来的。）
        if message.raw.get("text_from_file"):
            return True
        if message.is_self:
            return False
        return True

    def enabled(self) -> bool:
        """过滤开关。关掉之后所有消息都进待办（排查误杀时用）。

        和 quick_parse 的碎片过滤共用同一个三档开关：
        strict / loose = 开，off = 关。
        """
        return str(config.get("parse", "filter_strength", "strict")) != "off"
