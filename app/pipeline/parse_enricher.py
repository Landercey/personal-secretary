"""把 quick_parse 接成管线零件。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  改"哪些消息严判、哪些宽判"  → `_strict()`（这是最容易改错的一处）
  改解析规则本身              → 去 quick_parse.py，这个文件不含规则
  改推送里带的原文片段长度    → config.toml 的 [push] quote_max_chars
  改"碎片记不记进废件箱"      → `_record_dropped()`

=====================================================================
切分（order 10）：没有任何草稿时，从消息正文切出候选待办。
时间识别已经写在 quick_parse 里一次完成 —— 不为拆而拆，
拆成两个零件反而要来回传递中间状态。
"""

from __future__ import annotations

from ..contracts.interfaces import DraftEnricher
from ..contracts.models import SOURCE_QQ_INBOX, Message, TaskDraft
from ..runtime.config import config
from . import quick_parse


class RuleParseEnricher(DraftEnricher):
    """规则版切分 + 时间识别。零 Token、零联网、秒出结果。

    这是内置的默认理解零件。AI 增强是另一个零件（插件），排在它后面
    （order 90），负责"哪句才是真的待办"和"长文档在讲什么"。
    """

    name = "rule-parse"
    order = 10

    def process(self, message: Message, drafts: list[TaskDraft]) -> list[TaskDraft]:
        if drafts:
            return drafts          # 已经有草稿了（比如插件提前产出），不重复切
        strict = self._strict(message)
        text = message.text or ""
        found = quick_parse.parse_text(text, strict=strict)
        self._record_dropped(message, text)
        quote_len = int(config.get("push", "quote_max_chars", 80))
        for draft in found:
            draft.source_kind = message.source_kind
            draft.source_name = message.source_name
            draft.sender = message.sender
            draft.origin_msg_id = message.msg_id
            draft.attachments = list(message.attachments)
            draft.quoted = message.quote(quote_len)
        return found

    @staticmethod
    def _record_dropped(message: Message, text: str) -> None:
        """把被判掉的行记进 message.raw，由装配点决定要不要写废件箱。

        整段话（散文块）不逐行记 —— 那种情况下"每行都不是待办"是正常的，
        记下来只会把废件箱刷满噪声。
        """
        lines = [line for line in (text or "").splitlines() if line.strip()]
        if not lines or quick_parse.is_prose_block(lines):
            message.raw["dropped_lines"] = []
            return
        message.raw["dropped_lines"] = [
            line for line in lines if quick_parse.parse_line(line) is None
        ]

    @staticmethod
    def _strict(message: Message) -> bool:
        """别人的材料严判，自己随手记的宽判。

        证据（CONTRACT.md 第七节第 5 条）：
          "拿快递""打游戏" 是自己发给小号的合格短待办，一条都不许误伤。

        为什么不能用同一个标准：
          群里和截图里满是碎片（值班表、信号栏、门牌号），必须狠；
          但"拿快递"这种自己敲的三个字，按群里的标准会被当成"没内容"丢掉。
        两边不可能用同一把尺子。

        这里的"严"最终由 quick_parse.is_fragment() 执行，三档过滤强度
        （strict / loose / off）在 config.toml 的 [parse] filter_strength 改。
        """
        if message.is_self or message.source_kind == SOURCE_QQ_INBOX:
            return False
        strength = str(config.get("parse", "filter_strength", "strict"))
        return strength != "off"
