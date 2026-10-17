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
from ..contracts.models import SOURCE_QQ_GROUP, PushPayload, Task
from ..runtime.humanize import human_due


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
    """一条待办 → **一行** `标题｜时间｜群名`，手机上要能一眼扫过去、也能整段复制。

    2026-10-13 用户原话："推送的太繁琐了，改改"。以前那一行长这样：
        1. 交作业 — 2026-10-30 18:30:00（私聊 987654321（我发的））
                  ↳ 10 月 30 号 18:30 交作业
    猜都猜得到问题在哪：秒也写、来源里套括号、原话又跟标题重复一遍。
    2026-10-17 用户要的形态（照抄到滴答清单/Notion 里能直接用）：
        1. 交作业｜10-30 18:30｜某班群

    · 时间说人话（今天/明天/周几，超过一周才写月-日）；
    · 来源只给**群消息**加，而且是群名 —— 私聊/自己发的不加
      （"（私聊 987654321（我发的））"这种纯属噪音）；
    · 分隔符用全角竖线而不是括号：括号里的时间会被某些清单软件的文本识别吞掉，
      竖线一眼能看出"这是三块"，复制出去也好切；
    · 原话不再每条都附：它是从消息里抄出来的，跟标题基本重复。
      真要看原话，界面的详情里一直有（AI 归纳的那些除外，见 build_digest）。
    """
    when = human_due(task.due_at)[0]
    where = f" · {task.location}" if task.location else ""
    head = f"{index}. " if index else ""
    # confidence < 1 = 这条是 AI 归纳出来的，不是规则算出来的。
    # 必须让用户在推送里就看得出来，不然他没法判断"这是原文说的还是模型补的"。
    ai = "【AI】" if float(getattr(task, "confidence", 1.0) or 1.0) < 1 else ""
    parts = [f"{head}{task.title}{ai}", f"{when}{where}"]
    if task.source_kind == SOURCE_QQ_GROUP and task.source_name:
        parts.append(_short_source(str(task.source_name)))
    return "｜".join(parts)


def _short_source(name: str) -> str:
    """群名太长就砍一刀 —— 推送那一行放不下十几个字的名字。"""
    clean = " ".join(str(name or "").split())
    return clean if len(clean) <= 12 else clean[:12] + "…"


def _excerpt_line(task: Task) -> str:
    """要不要给这条附一句原话。**只给 AI 归纳的那些附。**

    规则算出来的标题就是从原话里摘的，附上只是把消息写长一倍；
    AI 归纳的标题可能跟原话差得远，那才需要原文在旁边作对照
    （合同里也要求这种情况必须标出来）。
    """
    quote = " ".join(str(task.quoted or "").split())
    if not quote or float(getattr(task, "confidence", 1.0) or 1.0) >= 1:
        return ""
    return f"   ↳ 原话：{quote[:60]}{'…' if len(quote) > 60 else ''}"


def build_digest(tasks: list[Task], title: str = "待办汇总",
                 max_attachments: int = 5, rest: int = 0) -> PushPayload:
    """一批待办 → 一条汇总推送。

    `max_attachments` 是安全阀：群机器人每分钟只能发 20 条，
    一天的汇总如果每条待办都带 3 张图，一秒钟就把额度用光，
    后面真正的提醒反而发不出去了。

    `rest` = "还有多少条不在这次汇总里"（更远的日子）。不写出来的话，
    用户会以为清单就这么几条、别的不见了。
    """
    if not tasks:
        body = "这段时间没有要做的。" + (f"\n（更远的还有 {rest} 条，界面里能看到）"
                                         if rest else "")
    else:
        lines = [format_task_line(t, i + 1) for i, t in enumerate(tasks)]
        quotes = [line for line in (_excerpt_line(t) for t in tasks) if line]
        tail = [""] + quotes if quotes else []
        # 有一条是 AI 归纳的，就在末尾说清楚（合同里写明必须标）
        if any(float(getattr(t, "confidence", 1.0) or 1.0) < 1 for t in tasks):
            tail += ["", "【AI 整理，可能有误】带【AI】的那几条是模型归纳的，"
                         "原文和附件都留在本地，点开详情能看到。"]
        if rest:
            tail += ["", f"（更远的还有 {rest} 条，界面里能看到）"]
        # 早报末尾提醒一句"能整段复制"：手机上把这条复制进滴答清单/Notion 是最顺的路。
        # 只给早报加 —— 一天三次都挂这句就是噪音了（用户嫌过推送繁琐）。
        if title.startswith("今天要做的"):
            tail += ["", "（长按这条消息可以整段复制）"]
        body = "\n".join(lines + tail)

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
