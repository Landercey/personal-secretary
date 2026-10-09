"""Python 和界面之间的桥。

网页那边用 `window.pywebview.api.xxx()` 调这里的方法，返回 JSON 能装的东西。
**界面不直接碰数据库** —— 数据一律走这里，规则留在上层。

=====================================================================
想改这里的话
=====================================================================
  界面要多显示一个字段   → 在 `_task_dict()` 里加上
  加一个新操作           → 写一个方法，然后在 index.html 里调它
  改列表分组             → Store.list_tasks() 的 view 参数
"""

from __future__ import annotations

from datetime import datetime

from ..contracts.models import STATUS_DONE, STATUS_DROPPED


def _human_due(due_at: str | None) -> tuple[str, str]:
    """把到期时间变成人话，返回 (文字, 紧急程度)。

    紧急程度给界面用来上色：overdue 红、today 橙、soon 蓝、later 灰、
    none 无时间。
    """
    if not due_at:
        return "没写时间", "none"
    try:
        moment = datetime.strptime(due_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return due_at, "later"

    today = datetime.now().date()
    day = moment.date()
    delta = (day - today).days
    clock = moment.strftime("%H:%M")

    if moment < datetime.now() and delta <= 0:
        return f"已逾期 · {moment.strftime('%m-%d %H:%M')}", "overdue"
    if delta == 0:
        # 当天 23:59 是我们表示"全天"的约定，显示成"今天"更好懂
        return ("今天" if clock == "23:59" else f"今天 {clock}"), "today"
    if delta == 1:
        return ("明天" if clock == "23:59" else f"明天 {clock}"), "soon"
    if delta < 7:
        weekday = "一二三四五六日"[day.weekday()]
        return f"周{weekday}" + ("" if clock == "23:59" else f" {clock}"), "soon"
    return moment.strftime("%m-%d %H:%M"), "later"


class Api:
    """网页能调到的全部方法。加方法不用改别的，网页那边直接调。"""

    def __init__(self, store) -> None:
        self.store = store
        # 网页轮询用：每次返回一个自增号，界面上"数据变了没"靠它判断
        self._revision = 0

    # ------------------------------------------------------------------ 读
    def tasks(self, view: str = "open") -> dict:
        """一个分组下的待办。

        view: open / today / tomorrow / **week（本周一~周日）** /
              **next_week（下周一~周日）** / overdue / archive / junk
        """
        rows = self.store.list_tasks(view, limit=500)
        items = []
        for task in rows:
            due_text, urgency = _human_due(task.due_at)
            items.append({
                "id": task.task_id,
                "title": task.title,
                "due": due_text,
                "urgency": urgency,
                "location": task.location or "",
                "source": task.source_name or "",
                "sender": task.sender or "",
                "quoted": task.quoted or "",
                "detail": task.detail or "",
                "remind": int(task.remind_level),
                "attachments": len(task.attachments),
                "status": task.status,
            })
        self._revision += 1
        return {"items": items, "revision": self._revision}

    def counts(self) -> dict:
        """左侧导航上的数字。"""
        return {
            "open": self.store.count_tasks("open"),
            "today": self.store.count_tasks("today"),
            "tomorrow": self.store.count_tasks("tomorrow"),
            "week": self.store.count_tasks("week"),
            "next_week": self.store.count_tasks("next_week"),
            "overdue": self.store.count_tasks("overdue"),
            "archive": self.store.count_tasks("archive"),
        }

    def stats(self) -> dict:
        return self.store.stats()

    def dropped(self) -> dict:
        """废件箱：没收下的原文。用来回答"我发的那条为什么没反应"。"""
        return {"items": self.store.list_dropped(limit=200)}

    # ------------------------------------------------------------------ 写
    def complete(self, task_id: int) -> dict:
        self.store.set_status(int(task_id), STATUS_DONE, "界面里标记完成")
        return {"ok": True}

    def drop(self, task_id: int) -> dict:
        """标记误判。留一笔记录，以后调规则用得上。"""
        self.store.set_status(int(task_id), STATUS_DROPPED, "界面里判定为误判")
        return {"ok": True}

    def archive(self, task_id: int) -> dict:
        self.store.archive(int(task_id), "界面里归档")
        return {"ok": True}

    # ------------------------------------------------------------------ 环境
    def config_summary(self) -> dict:
        """界面上显示"现在什么状态"用。"""
        from ..runtime.config import config
        from ..runtime.sysmon import describe as mem_describe

        return {
            "memory": mem_describe(),
            "qq_groups": len(config.get("qq", "group_ids", []) or []),
            "push_times": config.get("push", "times", []),
            "db": str(getattr(self.store, "conn", "")),
        }
