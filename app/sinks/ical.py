"""把待办导出成 `.ics` 日历文件 —— 手机日历、Outlook、Google 日历都认这个。

为什么值得做：很多人不用待办软件，但**手机上一定有日历**。
导进去之后到点会响，比"打开这个程序看一眼"更符合日常习惯。

只导出**写了时间**的待办：日历里没法放"没有时间的事"。
"""

from __future__ import annotations

import datetime as dt


def _escape(text: str) -> str:
    """iCal 的转义：反斜杠、分号、逗号、换行都有特殊含义。"""
    return (str(text or "")
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\r\n", "\n")
            .replace("\n", "\\n"))


def _fold(line: str) -> str:
    """一行超过 75 字节要折断，下一行开头补一个空格（iCal 的规定）。

    按**字节**算而不是按字算：一个汉字是 3 字节，按字算会超。
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, chunk = [], b""
    for ch in line:
        piece = ch.encode("utf-8")
        if len(chunk) + len(piece) > 73:
            out.append(chunk.decode("utf-8"))
            chunk = b""
        chunk += piece
    if chunk:
        out.append(chunk.decode("utf-8"))
    return "\r\n ".join(out)


def _stamp(value: dt.datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S")


def build(tasks, now: dt.datetime | None = None) -> str:
    """待办列表 → 一整份日历文件的内容（字符串）。

    `due_at` 是本地时间（`YYYY-MM-DD HH:MM`），写成**不带时区**的浮动时间 ——
    手机在哪个时区就按哪里显示，用户不用管。
    """
    now = now or dt.datetime.now()
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//personal-secretary//待办//CN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:私人秘书 待办",
        "METHOD:PUBLISH",
    ]
    for task in tasks:
        raw = str(getattr(task, "due_at", "") or "").strip()
        if not raw:
            continue
        try:
            start = dt.datetime.strptime(raw[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        detail = str(getattr(task, "quoted", "") or getattr(task, "detail", "") or "")
        source = str(getattr(task, "source_name", "") or "")
        lines += [
            "BEGIN:VEVENT",
            # UID 用待办编号：重复导入同一份不会长出几十个一模一样的日程
            f"UID:todo-{getattr(task, 'task_id', 0)}@personal-secretary",
            f"DTSTAMP:{now.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART:{_stamp(start)}",
            f"DTEND:{_stamp(start + dt.timedelta(minutes=30))}",
            f"SUMMARY:{_escape(getattr(task, 'title', '') or '待办')}",
        ]
        if getattr(task, "location", ""):
            lines.append(f"LOCATION:{_escape(task.location)}")
        body = "\n".join(x for x in (detail[:500], f"来自：{source}" if source else "") if x)
        if body:
            lines.append(f"DESCRIPTION:{_escape(body)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"
