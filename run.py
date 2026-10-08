"""开发期命令行入口。

第 9 步会被图形界面取代，但在那之前它是验证链路唯一的工具 ——
而且它是**唯一**一个允许 import 各模块来组装程序的文件（装配点），
别的模块之间不许互相 import。

用法：
    python run.py "明天下午三点交作业"     录入一条
    python run.py --list                   看未完成
    python run.py --list junk              看废件箱
    python run.py --done 3                 标记完成
    python run.py --digest                 生成一份汇总并打印
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
from app.sinks.console import ConsoleSink, build_digest          # noqa: E402
from app.sources.manual import ManualSource                      # noqa: E402
from app.store.repository import SqliteStore, open_database      # noqa: E402


def build(database: str | Path | None = None):
    """把零件装配起来。换数据库路径主要是为了测试。"""
    db_path = Path(database) if database else default_data_dir() / "secretary.db"
    store = SqliteStore(open_database(db_path))
    source = ManualSource()
    pipeline = Pipeline(draft_enrichers=[RuleParseEnricher()])
    sink = ConsoleSink()
    return store, source, pipeline, sink


def ingest(store, source, pipeline, text: str) -> int:
    """一段文字 → 待办。返回新入库的条数。"""
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

    if args.digest:
        tasks = store.list_tasks("open")
        sink.deliver(build_digest(tasks, title="今天要做的"))
        return 0

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
    """第 1 步的门禁：契约完整、配置齐全、目录能建。"""
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
