"""开发期命令行入口。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  加一个新命令                    → 在 `main()` 里加一个 argparse 参数和分支
  改零件怎么装配（注册新插件）    → `build()` —— 全项目只有这里能 import 各模块
  改录入之后做什么（比如顺手推送） → `ingest()`
  改自检检查哪几项                → `_selftest()`

**这个文件是全项目唯一的"装配点"。** 别的模块之间不许互相 import
（由 tests/test_contract_integrity.py 自动检查）。想加一个新零件，
就是在这里 `new` 出来、塞进 Pipeline。

=====================================================================
第 9 步会被图形界面取代，但在那之前它是验证链路唯一的工具 ——
而且它是**唯一**一个允许 import 各模块来组装程序的文件（装配点），
别的模块之间不许互相 import。

用法：
    python run.py "明天下午三点交作业"     录入一条
    python run.py --list                   看未完成
    python run.py --list junk              看废件箱
    python run.py --done 3                 标记完成
    python run.py --digest                 生成一份汇总并打印到控制台
    python run.py --push                   把汇总真正推送到企业微信
    python run.py --check-push             检查推送通道配置对不对（不发消息）
    python run.py --stats                  看统计
    python run.py --selftest               检查契约与环境
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.contracts.models import STATUS_DONE, STATUS_DROPPED     # noqa: E402
from app.pipeline.chain import Pipeline                          # noqa: E402
from app.pipeline.parse_enricher import RuleParseEnricher        # noqa: E402
from app.runtime.config import config, default_data_dir          # noqa: E402
from app.sinks.console import build_digest                       # noqa: E402
from app.sinks.router import build_sink, channel_status          # noqa: E402
from app.sources.manual import ManualSource                      # noqa: E402
from app.store.repository import SqliteStore, open_database      # noqa: E402


def build(database: str | Path | None = None):
    """把零件装配起来。换数据库路径主要是为了测试。

    加一个新零件/插件的标准动作就是改这里（下面四行）：
        store / source / pipeline / sink
    比如以后接 QQ 来源，就是在 source 那边多一个 QQSource；
    加 AI 理解，就是 pipeline 的 draft_enrichers 里多一个 AISummarizer。
    """
    db_path = Path(database) if database else default_data_dir() / "secretary.db"
    store = SqliteStore(open_database(db_path))
    source = ManualSource()
    pipeline = Pipeline(draft_enrichers=[RuleParseEnricher()])
    sink = build_sink()          # 配了企微就走企微，没配就退回控制台
    return store, source, pipeline, sink


def ingest(store, source, pipeline, text: str) -> int:
    """一段文字 → 待办。返回新入库的条数。

    这就是整条流水线的样子，一共五步：
      1. 来源产出 Message
      2. 存原始消息（留证据，出问题能回查）
      3. 理解层跑一遍：消息 → 草稿
      4. 草稿入库（重复的会被自动挡掉）
      5. 没收下的碎片记进废件箱
    """
    message = source.from_text(text)
    store.save_message(message)
    prepared, drafts = pipeline.run(message)
    stored = store.save_drafts(prepared, drafts)

    # 被判为碎片/噪声的行记一笔：不推送，但留证据。
    # 由解析器告诉我们具体是哪几行（见 RuleParseEnricher._record_dropped），
    # 装配点只负责落库，不自己猜。
    for line in prepared.raw.get("dropped_lines", []):
        store.log_dropped_text(line, "碎片：摘掉时间后没内容",
                               prepared.source_kind, prepared.source_name, prepared.msg_id)
    store.purge_old_dropped()
    return len(stored)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="私人秘书（开发期命令行）")
    parser.add_argument("text", nargs="*", help="要录入的文字")
    parser.add_argument("--list", nargs="?", const="open", default=None,
                        help="列出待办：open/today/tomorrow/week/overdue/archive/junk")
    parser.add_argument("--done", type=int, help="把某条标记为完成")
    parser.add_argument("--drop", type=int, help="把某条标记为误判（日后调规则的案例）")
    parser.add_argument("--digest", action="store_true", help="生成汇总并打印")
    parser.add_argument("--push", action="store_true", help="生成汇总并真正推送")
    parser.add_argument("--check-push", action="store_true",
                        help="检查推送通道配置（不发消息）")
    parser.add_argument("--stats", action="store_true", help="看统计")
    parser.add_argument("--selftest", action="store_true", help="检查契约与环境")
    parser.add_argument("--db", help="数据库路径（默认 data/secretary.db）")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    store, source, pipeline, sink = build(args.db)

    if args.done:
        store.set_status(args.done, STATUS_DONE, "用户在命令行标记完成")
        print(f"已标完成：#{args.done}")
        return 0

    if args.drop:
        store.set_status(args.drop, STATUS_DROPPED, "用户判定为误判")
        print(f"已标误判：#{args.drop}")
        return 0

    if args.stats:
        for key, value in store.stats().items():
            print(f"{key:>10}: {value}")
        return 0

    if args.check_push:
        print("推送通道现状：")
        for line in channel_status():
            print(f"  · {line}")
        print("\n要真正收到推送，需要在 config.toml 里填 [wecom_bot] webhook，"
              "并把 enabled 改成 true。")
        return 0

    if args.digest or args.push:
        tasks = store.list_tasks("open")
        payload = build_digest(tasks, title="今天要做的")
        if args.digest:
            # --digest 永远只打印，不真的发出去（免得调试时误发到群里）
            from app.sinks.console import ConsoleSink

            ConsoleSink().deliver(payload)
            return 0
        payload.channel = sink.name
        result = sink.deliver(payload)
        explain = getattr(sink, "explain", None)
        print(explain(result) if explain else f"推送{'成功' if result.ok else '失败'}：{result.message}")
        return 0 if result.ok else 1

    if args.list is not None:
        if args.list == "junk":
            # 废件箱在独立的表里（它是"没被收下的原文"，不是待办）。
            # 留着是为了翻案和调规则 —— CONTRACT.md 第七节第 4 条。
            items = store.list_dropped()
            if not items:
                print("（废件箱是空的）")
                return 0
            print(f"废件箱（{len(items)} 条）—— 这些是没被收下的原文：")
            for item in items:
                origin = f"  ← {item['source_name']}" if item["source_name"] else ""
                print(f"  · {item['text'][:60]}   [{item['reason'][:12]}]{origin}")
            return 0
        tasks = store.list_tasks(args.list)
        if not tasks:
            print(f"（{args.list} 没有内容）")
            return 0
        for task in tasks:
            mark = {"open": "○", "done": "✔", "dropped": "✕"}.get(task.status, "?")
            due = task.due_at or "没写时间"
            origin = f"  ← {task.source_name}" if task.source_name else ""
            print(f"{mark} #{task.task_id:<4} {task.title}   [{due}]{origin}")
        return 0

    text = " ".join(args.text).strip()
    if not text:
        parser.print_help()
        return 0

    added = ingest(store, source, pipeline, text)
    print(f"\n录入完成：新待办 {added} 条（共 {store.count_tasks('open')} 条未完成）")
    store_dump = store.list_tasks("open", limit=5)
    for task in store_dump:
        print(f"  #{task.task_id:<4} {task.title}   [{task.due_at or '没写时间'}]")
    return 0


def _selftest() -> int:
    """第 1 步的门禁：契约完整、配置齐全、目录能建。

    加一个新的必填配置项时，记得往下面的 `required` 里加一行 ——
    否则用户配置漏了会在运行到一半时才炸，而不是启动时就告诉他。
    """
    from app.contracts import models
    from app.contracts.interfaces import (
        DraftEnricher, Sink, Source, Store, TextEnricher,
    )

    problems: list[str] = []
    for name in ("Message", "TaskDraft", "Task", "PushPayload", "Attachment"):
        if not hasattr(models, name):
            problems.append(f"契约缺数据格式：{name}")
    for iface in (Source, TextEnricher, DraftEnricher, Store, Sink):
        if not getattr(iface, "__abstractmethods__", None):
            problems.append(f"接口没有抽象方法：{iface.__name__}")

    required = {
        "parse": ["filter_strength", "confidence_min", "merge_window_seconds", "due_default_hour"],
        "title": ["max_len", "min_meaningful_chars"],
        "store": ["attachment_retention_days", "keep_dropped_days"],
        "push": ["default_times", "quote_max_chars", "fallback_text_chars"],
        "ai": ["mode", "cloud_priority", "token_budget_per_day"],
        "resource": ["mem_critical_percent", "mem_resume_percent"],
        "risk": ["require_secondary_account"],
    }
    for section, keys in required.items():
        for key in keys:
            if config.get(section, key, None) is None:
                problems.append(f"配置缺项：{section}.{key}")

    if config.load_error:
        problems.append(config.load_error)

    data_dir = default_data_dir()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        problems.append(f"数据目录建不了：{exc}")

    print("私人秘书 · 自检")
    print(f"  项目根目录：{config.path.parent if config.path else '?'}")
    print(f"  配置文件：  {config.path}")
    print(f"  数据目录：  {data_dir}")
    print(f"  过滤强度：  {config.get('parse', 'filter_strength')}")
    print(f"  AI 模式：   {config.get('ai', 'mode')}（云端优先={config.get('ai', 'cloud_priority')}）")
    if problems:
        print("\n发现问题：")
        for item in problems:
            print(f"  ✕ {item}")
        return 1
    print("\n✓ 契约完整，配置齐全")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
