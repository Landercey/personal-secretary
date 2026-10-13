"""把机器格式的东西说成人话。界面上和推送里共用同一份 —— 两处各写一份迟早会不一致。

现在只装了一个：到期时间。界面拿它上色，推送拿它当那一行的时间。
"""

from __future__ import annotations

from datetime import datetime

from ..contracts.models import TIME_FORMAT


def human_due(due_at: str | None) -> tuple[str, str]:
    """到期时间 → (人话, 紧急程度)。

    紧急程度给界面用来上色：overdue 红、today 橙、soon 蓝、later 灰、none 无时间。

    "23:59" 是我们表示"只说了哪天"的约定（见 parse.due_default_hour），
    显示成"今天/明天"比"今天 23:59"更像人话。
    """
    if not due_at:
        return "没写时间", "none"
    try:
        moment = datetime.strptime(str(due_at), TIME_FORMAT)
    except ValueError:
        return str(due_at), "later"

    today = datetime.now().date()
    day = moment.date()
    delta = (day - today).days
    clock = moment.strftime("%H:%M")

    if moment < datetime.now() and delta <= 0:
        return f"已逾期 · {moment.strftime('%m-%d %H:%M')}", "overdue"
    if delta == 0:
        return ("今天" if clock == "23:59" else f"今天 {clock}"), "today"
    if delta == 1:
        return ("明天" if clock == "23:59" else f"明天 {clock}"), "soon"
    if delta < 7:
        weekday = "一二三四五六日"[day.weekday()]
        return f"周{weekday}" + ("" if clock == "23:59" else f" {clock}"), "soon"
    return moment.strftime("%m-%d %H:%M"), "later"
