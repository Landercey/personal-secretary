"""把理解层的零件串成一条管线。

固定顺序执行（CONTRACT.md 第四节）：
  消息级零件（按 order 从小到大）→ 草稿级零件（按 order 从小到大）

这里也负责"碎片进废件箱"这一步：零件返回 None 表示这行是碎片/噪声，
由管线统一记一笔日志，而不是各零件自己写库 —— 存储归存储，规则归规则。
"""

from __future__ import annotations

from typing import Iterable

from ..contracts.interfaces import DraftEnricher, TextEnricher
from ..contracts.models import Message, TaskDraft


class Pipeline:
    """理解管线。零件可以随时增删，顺序由 order 决定。"""

    def __init__(
        self,
        text_enrichers: Iterable[TextEnricher] = (),
        draft_enrichers: Iterable[DraftEnricher] = (),
    ) -> None:
        self.text_enrichers = sorted(text_enrichers, key=lambda e: e.order)
        self.draft_enrichers = sorted(draft_enrichers, key=lambda e: e.order)

    # ------------------------------------------------------------------ 注册
    def add_text(self, enricher: TextEnricher) -> None:
        self.text_enrichers.append(enricher)
        self.text_enrichers.sort(key=lambda e: e.order)

    def add_draft(self, enricher: DraftEnricher) -> None:
        self.draft_enrichers.append(enricher)
        self.draft_enrichers.sort(key=lambda e: e.order)

    # ------------------------------------------------------------------ 执行
    def prepare(self, message: Message) -> Message:
        """跑消息级零件：文件解析、语音转写、AI 读图。失败就跳过，不中断。"""
        current = message
        for enricher in self.text_enrichers:
            if not _safe_enabled(enricher):
                continue
            try:
                current = enricher.process(current) or current
            except Exception as exc:  # 单个零件出问题不能把整条管线带崩
                current.raw.setdefault("enricher_errors", []).append(
                    f"{getattr(enricher, 'name', '?')}: {type(exc).__name__}"
                )
        return current

    def extract(self, message: Message, drafts: list[TaskDraft] | None = None) -> list[TaskDraft]:
        """跑草稿级零件：切分 → 时间识别 → 去重 → AI 增强。"""
        current = list(drafts or [])
        for enricher in self.draft_enrichers:
            if not _safe_enabled(enricher):
                continue
            try:
                current = enricher.process(message, current) or []
            except Exception as exc:
                message.raw.setdefault("enricher_errors", []).append(
                    f"{getattr(enricher, 'name', '?')}: {type(exc).__name__}"
                )
        return current

    def run(self, message: Message) -> tuple[Message, list[TaskDraft]]:
        """完整跑一遍：消息进去，草稿出来。"""
        prepared = self.prepare(message)
        return prepared, self.extract(prepared)


def _safe_enabled(enricher) -> bool:
    """零件自己判断开关时崩了，当作关闭处理。"""
    try:
        return bool(enricher.enabled())
    except Exception:
        return False
