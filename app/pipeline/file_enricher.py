"""理解层零件：把附件里的文字读出来，拼进消息正文。

=====================================================================
它在管线里的位置
=====================================================================
    消息进来
      ↓
    【本零件】把附件（图片/Word/PPT/PDF）读成文字，追加到 message.text
      ↓
    闲聊过滤 → 时间识别/切分 → 入库 → 推送

它只改 `message.text`，**不产生待办**。这样设计的好处：
一张"明天交作业"的黑板照，读出来的字和直接打字发过来的消息，
走的是完全相同的那条链路 —— 规则只需要维护一套。

⚠️ **必须跑在闲聊过滤之前**（管线里就是这样的：消息级零件整组先跑）。
  反过来的话，一条正文只有"看这个"的群消息会被判成
  "太短、没有行动词"直接丢掉，图里的作业要求就永远看不见了。

=====================================================================
想改这里的话
=====================================================================
  改文件解析本身            → extract/ 目录
  改"读出来的字怎么拼进正文" → `_append_text()`
  改"附件读不出字怎么办"     → `_fallback_note()`
  改单个附件大小上限         → config.toml 的 [extract] max_attachment_mb
"""

from __future__ import annotations

from ..contracts.interfaces import TextEnricher
from ..contracts.models import KIND_IMAGE, Message
from ..runtime.config import config
from .extract import extract_text, is_supported


class FileEnricher(TextEnricher):
    """把附件里的文字读出来拼进正文。

    管线里「消息级零件」整组跑在「草稿级零件」之前，所以它是
    **先于闲聊过滤**执行的 —— 这是刻意的，见模块开头的说明。

    order=10 是消息级组内的排序，为以后加"语音转写"之类的零件留位置。
    """

    name = "file-extract"
    order = 10

    def process(self, message: Message) -> Message:
        if not message.attachments:
            return message

        limit_bytes = self._size_limit_bytes()
        parts: list[str] = []
        notes: list[str] = []

        for item in message.attachments:
            if not item.path:
                continue
            if not is_supported(item.path):
                continue
            if item.size and item.size > limit_bytes:
                notes.append(f"{item.name}：文件太大（{item.size // 1024 // 1024} MB），跳过")
                continue

            result = extract_text(item.path)
            if result.ok and result.text.strip():
                parts.append(result.text.strip())
                notes.append(f"{item.name}：{result.note}")
            else:
                notes.append(f"{item.name}：{result.note or '没读出文字'}")

        if parts:
            self._append_text(message, "\n".join(parts))
        else:
            note = self._fallback_note(message)
            if note:
                self._append_text(message, note)

        message.raw["extract_notes"] = notes
        return message

    # ------------------------------------------------------------------
    @staticmethod
    def _size_limit_bytes() -> int:
        try:
            mb = float(config.get("extract", "max_attachment_mb", 30) or 30)
        except (TypeError, ValueError):
            mb = 30.0
        return int(mb * 1024 * 1024)

    @staticmethod
    def _append_text(message: Message, text: str) -> None:
        """把读出来的字拼进正文。

        放在**前面**：原消息带的文字往往是"帮我看下这个"这种废话，
        附件里的才是正事。放前面能让后面的切分优先看到真正的内容。
        """
        original = (message.text or "").strip()
        message.text = f"{text}\n{original}".strip() if original else text

    @staticmethod
    def _fallback_note(message: Message) -> str:
        """一个字都没读出来时，要不要留一句说明。

        规则（沿用旧项目的结论）：
          · 自己发到小号的（收件箱）→ 留一条占位，你至少知道有这回事，
            能点回 QQ 自己看；
          · 群里别人发的 → **不留**。不然群里每张表情包都会变成一条待办。

        为什么区分：群消息量大、图多，留占位等于把待办列表刷满；
        收件箱是你专门发给自己的，每条都值得留。
        """
        if not message.is_self:
            return ""
        images = [a for a in message.attachments if a.kind == KIND_IMAGE]
        if not images:
            return ""
        name = images[0].name or "图片"
        return f"图片 {name}（没认出文字，自己点开看）"
