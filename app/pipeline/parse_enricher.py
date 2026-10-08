"""把 quick_parse 接成管线零件。

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
    """规则版切分 + 时间识别。零 Token、零联网、秒出结果。"""

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
        """
        if message.is_self or message.source_kind == SOURCE_QQ_INBOX:
            return False
        strength = str(config.get("parse", "filter_strength", "strict"))
        return strength != "off"
