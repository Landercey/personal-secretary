"""控制台输出：开发期用来肉眼确认链路通不通（CONTRACT.md 第九步）。

它同时是**最后的兜底通道** —— 企微没配好时，消息至少打印在屏幕上，
而不是静默丢掉。见 sinks/router.py 的 build_sink()。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  改推送文案长什么样      → `build_digest()`（企微推送共用同一个函数，
                            所以改这里等于两边一起改，不会不一致）
  改单条待办那一行的格式  → `format_task_line()`
  改"没有待办时说什么"    → `build_digest()` 里的第一行

=====================================================================
"""

from __future__ import annotations

from ..contracts.interfaces import Sink, SinkResult
from ..contracts.models import PushPayload, Task


class ConsoleSink(Sink):
    name = "console"

    def deliver(self, payload: PushPayload) -> SinkResult:
        print(f"\n===== 推送到 {payload.channel} =====")
        if payload.title:
            print(payload.title)
        if payload.body:
            print(payload.body)
        if payload.note:
            print(f"[提示] {payload.note}")
        print("=" * 28)
        return SinkResult(ok=True, message="已打印到控制台")


def format_task_line(task: Task, index: int = 0) -> str:
    """一条待办 → 一行人话。推送和列表共用同一个格式，避免两处不一致。"""
    when = task.due_at or "没写时间"
    where = f" @{task.location}" if task.location else ""
    origin = f"（{task.source_name}）" if task.source_name else ""
    head = f"{index}. " if index else ""
    return f"{head}{task.title} — {when}{where}{origin}"


def build_digest(tasks: list[Task], title: str = "待办汇总",
                 max_attachments: int = 5) -> PushPayload:
    """一批待办 → 一条汇总推送。

    `max_attachments` 是安全阀：群机器人每分钟只能发 20 条，
    一天的汇总如果每条待办都带 3 张图，一秒钟就把额度用光，
    后面真正的提醒反而发不出去了。
    """
    if not tasks:
        body = "现在没有待办。"
    else:
        lines = [format_task_line(t, i + 1) for i, t in enumerate(tasks)]
        quotes = [f"   ↳ {t.quoted}" for t in tasks if t.quoted]
        body = "\n".join(lines + ([""] + quotes if quotes else []))

    # 附件：只带前面几条的，且只带真实存在的文件。
    # 顺带去重 —— 同一张图被挂在两条待办上是常见的。
    attachments = []
    seen: set[str] = set()
    for task in tasks:
        for item in task.attachments:
            if len(attachments) >= max_attachments:
                break
            if not item.path or item.path in seen or not item.exists():
                continue
            seen.add(item.path)
            attachments.append(item)
        if len(attachments) >= max_attachments:
            break

    return PushPayload(channel="console", kind="text", title=title,
                       body=body, attachments=attachments)
