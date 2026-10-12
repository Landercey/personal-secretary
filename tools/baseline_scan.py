"""碎片率基线测量：把"改前 16%"和"改后 <3%"变成可重复测量的数字。

为什么需要它：
  "经常只有时间"这种主观抱怨没法验收，必须有一个数字。这个脚本给两个数字：

    --legacy <json>   旧库导出的标题列表 → 改前基线（实测 16%）
    --db <sqlite>     新项目的库      → 改后现状（要求 <3%）

  真数据（含群名、QQ 号）不进仓库，只放在 Work 目录，见 CONTRACT.md 第十节。

用法：
    python tools/baseline_scan.py --legacy "旧的待办标题.json" --db "D:\\私人秘书\\data\\secretary.db"
    python tools/baseline_scan.py --db data/secretary.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.pipeline.quick_parse import is_fragment, text_without_time   # noqa: E402

# 改前基线（旧库 168 条实测）。
TARGET_PERCENT = 3.0
BASELINE_PERCENT = 16.0


def rate(titles: list[str]) -> tuple[int, int, float]:
    bad = [t for t in titles if is_fragment(t)]
    total = len(titles)
    percent = (100.0 * len(bad) / total) if total else 0.0
    return len(bad), total, percent


def load_legacy(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("tasks", [])
    return [str(item.get("title", "")) for item in data if isinstance(item, dict)]


def load_db(path: Path) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute("SELECT title FROM tasks").fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


def report(label: str, titles: list[str]) -> bool:
    bad, total, percent = rate(titles)
    print(f"\n=== {label} ===")
    print(f"总数 {total}，碎片 {bad}，碎片率 {percent:.1f}%")
    if bad:
        print("碎片样本（最多 15 条）：")
        for title in [t for t in titles if is_fragment(t)][:15]:
            rest = text_without_time(title) or "（什么都不剩）"
            print(f"  · {title!r:<46} 剩：{rest!r}")
    ok = percent < TARGET_PERCENT
    verdict = "达标" if ok else "未达标"
    print(f"目标 <{TARGET_PERCENT}% → {verdict}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="碎片率测量")
    parser.add_argument("--legacy", help="旧库导出的标题 JSON（改前基线）")
    parser.add_argument("--db", help="新项目的 sqlite 文件（改后现状）")
    args = parser.parse_args()

    if not args.legacy and not args.db:
        parser.print_help()
        return 0

    all_ok = True
    if args.legacy:
        titles = load_legacy(Path(args.legacy))
        report(f"改前基线（旧库导出，期望约 {BASELINE_PERCENT}%）", titles)
        # 基线本来就高，不参与达标判定。
    if args.db:
        all_ok = report("改后现状（新项目库）", load_db(Path(args.db))) and all_ok
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
