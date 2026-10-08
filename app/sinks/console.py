"""控制台输出：开发期用来肉眼确认链路通不通（CONTRACT.md 第九步）。"""

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


def build_digest(tasks: list[Task], title: str = "待办汇总") -> PushPayload:
    """一批待办 → 一条汇总推送。"""
    if not tasks:
        body = "现在没有待办。"
    else:
        lines = [format_task_line(t, i + 1) for i, t in enumerate(tasks)]
        quotes = [f"   ↳ {t.quoted}" for t in tasks if t.quoted]
        body = "\n".join(lines + ([""] + quotes if quotes else []))
    return PushPayload(channel="console", kind="text", title=title, body=body)
