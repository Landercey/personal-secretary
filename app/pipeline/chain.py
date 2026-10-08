"""把理解层的零件串成一条管线。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  调整零件的先后顺序      → 改那个零件的 `order`（数字小的先跑），别改本文件
  加一个新的理解步骤      → 写一个类实现 TextEnricher / DraftEnricher，
                            然后在 run.py 的 build() 里注册进去
  改"某个零件崩了怎么办"  → `_safe_enabled()` 和两处 try/except
  临时关掉某一步          → 那个零件的 `enabled()` 返回 False

本文件保持"薄"是有意的：它只管顺序和容错，不含任何业务规则。
业务规则在各自的零件文件里。

=====================================================================
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
    """理解管线。零件可以随时增删，顺序由 order 决定。

    用法：
        pipe = Pipeline(draft_enrichers=[RuleParseEnricher()])
        message, drafts = pipe.run(message)
    """

    def __init__(
        self,
        text_enrichers: Iterable[TextEnricher] = (),
        draft_enrichers: Iterable[DraftEnricher] = (),
    ) -> None:
        self.text_enrichers = sorted(text_enrichers, key=lambda e: e.order)
        self.draft_enrichers = sorted(draft_enrichers, key=lambda e: e.order)

    # ------------------------------------------------------------------ 注册
    def add_text(self, enricher: TextEnricher) -> None:
        """注册一个消息级零件（文件解析、语音转写…），自动按 order 插好位置。"""
        self.text_enrichers.append(enricher)
        self.text_enrichers.sort(key=lambda e: e.order)

    def add_draft(self, enricher: DraftEnricher) -> None:
        """注册一个草稿级零件（切分、去重、AI…），自动按 order 插好位置。"""
        self.draft_enrichers.append(enricher)
        self.draft_enrichers.sort(key=lambda e: e.order)

    # ------------------------------------------------------------------ 执行
    def prepare(self, message: Message) -> Message:
        """跑消息级零件：文件解析、语音转写、AI 读图。

        单个零件出错**不中断**：记进 message.raw 里，继续跑下一个。
        理由：一个插件坏掉不该让整条链路停摆（CONTRACT.md 第二节硬性约束）。
        """
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
        """跑草稿级零件：切分 → 时间识别 → 去重 → AI 增强。

        传入 drafts 可以接着别人已经产出的草稿往下加工
        （插件先切好、再由规则去重，这种组合是允许的）。
        """
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
    """零件自己判断开关时崩了，当作关闭处理。

    一个零件的 enabled() 抛异常，不该让整条管线起不来。
    """
    try:
        return bool(enricher.enabled())
    except Exception:
        return False
