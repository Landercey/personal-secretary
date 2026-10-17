"""屏蔽词：标题里出现你设置的词，就不收这条。

群里天天有人发"砍一刀""拼多多""代写"这类东西 —— 它们看着像"事"，其实是广告。
这个插件只做一件事：把这些词命中后**直接扔掉**（原文照样进废件箱，能捡回来）。

它同时是**插件写法的第二个示例**（第一个是 `_template.py`）：演示
  · 插件清单 `PLUGIN`（版本、权限、自己那一段设置）；
  · 设置面板自动出现「插件 → 屏蔽词」，作者不用碰前端；
  · 把"我做了什么"写进结构化日志（设置 → 意见反馈 → 诊断包里能看到）。
"""

from __future__ import annotations

from app.contracts.interfaces import DraftEnricher
from app.runtime import logger
from app.runtime.config import config

TITLE = "屏蔽词"
DESCRIPTION = "标题里出现你设置的词就不收（砍一刀、代写…）。在设置 → 插件 → 屏蔽词 里配。"
CONFIG_SECTION = "mute"

PLUGIN = {
    "api": 1,
    "version": "1.0.0",
    # 不联网、不读文件、不把内容发出去 —— 权限声明成空的，安装时一眼能看见
    "permissions": [],
    "settings": {
        "mute": {
            "title": "屏蔽词",
            "desc": "标题里带这些词就不收（用逗号分开）",
            "fields": {"words": "砍一刀,拼多多,代写"},
            "comments": {
                "words": "屏蔽词（用逗号分开），留空 = 不屏蔽\n"
                         "#   例：砍一刀,拼多多,代写,刷单\n"
                         "#   只按标题判断；原文仍然会进废件箱，随时能捡回来。",
            },
        },
    },
}


class MuteWords(DraftEnricher):
    """草稿级零件：把命中屏蔽词的草稿扔掉。"""

    name = "mute-words"
    order = 15          # 规则解析（10）之后、AI（90）之前

    def enabled(self) -> bool:
        return bool(str(config.get("mute", "words", "") or "").strip())

    def process(self, message, drafts):
        words = [w.strip() for w in
                 str(config.get("mute", "words", "")).replace("，", ",").split(",")
                 if w.strip()]
        if not words:
            return drafts
        kept = []
        for draft in drafts:
            hit = next((w for w in words if w in (draft.title or "")), "")
            if hit:
                # 扔了也要留痕：不然用户只会觉得"我这条怎么没了"
                message.raw.setdefault("muted", []).append(draft.title)
                logger.note("info", "PLUGIN.LOG", stage="understand",
                            detail=f"屏蔽词「{hit}」命中，丢弃：{draft.title[:40]}",
                            plugin=self.name, msg_id=getattr(message, "msg_id", ""))
                continue
            kept.append(draft)
        return kept


def register(registry) -> None:
    """装配时调用一次。只注册，别联网/开线程。"""
    registry.add_draft(MuteWords())
