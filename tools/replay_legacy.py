"""把旧库里的**真实输入**重放一遍，量新旧解析器的差距。

=====================================================================
为什么需要这个工具
=====================================================================
「碎片率 17.6% → 3%」这种指标，如果只拿旧库里已经生成好的标题去量，
量的是"旧解析器的产出"，不是"新解析器会产出什么"。
真正的对比必须**重放输入**：

    旧库 content_units.raw_text      ← 每条消息的原文
    旧库 attachments.extracted_text  ← 每张图/每份文件 OCR 出来的文字

把这两样喂给新解析器，看它会产出什么。这才是可重复、可信的对比。

=====================================================================
用法
=====================================================================
    python tools/replay_legacy.py --old-db "D:\\旧的待办助手\\data\\todo.db"
    python tools/replay_legacy.py --old-db "…" --show 20

旧库里的群名、QQ 号都是真实信息，所以这个工具**只读**，不往外写文件。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.pipeline import quick_parse as q            # noqa: E402


def load_inputs(db_path: Path) -> list[dict]:
    """把旧库里"当时喂给解析器的东西"凑出来。

    两类：
      · 纯文字消息 → content_units.raw_text
      · 图片/文件   → attachments.extracted_text（OCR / 解析结果）

    图片消息的 raw_text 是 "[图片]" 这种占位符，没有信息量，
    真正的内容在 attachments.extracted_text 里。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        items: list[dict] = []
        for row in conn.execute("select * from content_units"):
            unit = dict(row)
            text = str(unit.get("raw_text") or "").strip()
            kind = str(unit.get("source_kind") or "")
            # 这个会话是自己发出去的吗（收件箱）—— 决定严判还是宽判
            is_self = bool(unit.get("from_inbox")) if "from_inbox" in unit else False
            items.append({"text": text, "source_kind": kind, "is_self": is_self,
                          "from_file": False, "origin": f"unit#{unit['id']}"})

        for row in conn.execute("select * from attachments"):
            att = dict(row)
            text = str(att.get("extracted_text") or "").strip()
            if not text:
                continue
            items.append({"text": text, "source_kind": "file", "is_self": True,
                          "from_file": True, "origin": f"att#{att['id']}"})
        return items
    finally:
        conn.close()


def replay(items: list[dict]) -> tuple[list[str], list[dict]]:
    """把每条输入喂给新解析器，收集产出的标题。

    返回 (全部产出的标题, 有问题的那几条)。
    """
    titles: list[str] = []
    problems: list[dict] = []
    for item in items:
        text = item["text"]
        if not text:
            continue
        # 跟真实链路保持一致：群消息严判，收件箱/手动宽判，文件内容严判
        strict = bool(item["from_file"]) or not item["is_self"]
        try:
            drafts = q.parse_text(text, strict=strict, from_file=item["from_file"])
        except Exception as exc:                     # 解析崩了本身就是问题
            problems.append({**item, "title": "", "why": f"解析异常：{type(exc).__name__}"})
            continue
        for draft in drafts:
            titles.append(draft.title)
            if q.is_fragment(draft.title):
                problems.append({**item, "title": draft.title, "why": "产出的是碎片"})
    return titles, problems


def main() -> int:
    parser = argparse.ArgumentParser(description="重放旧库输入，量新解析器的质量")
    parser.add_argument("--old-db", required=True, help="旧项目的 data/todo.db")
    parser.add_argument("--show", type=int, default=10, help="最多展示几条问题样本")
    args = parser.parse_args()

    db_path = Path(args.old_db)
    if not db_path.is_file():
        print(f"找不到旧库：{db_path}")
        return 1

    items = load_inputs(db_path)
    titles, problems = replay(items)
    total = len(titles)

    print("=" * 70)
    print("  旧库真实输入 → 新解析器   重放结果")
    print("=" * 70)
    print(f"  输入条数（含附件提取的文字）：{len(items)}")
    print(f"  新解析器产出待办：            {total}")
    print(f"  产出里是碎片的：              {len(problems)}"
          f"（{(100.0 * len(problems) / total) if total else 0:.1f}%）")
    print()

    # 旧库当时实际存下来的标题，作为对照
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        old_titles = [str(r[0]) for r in conn.execute("select title from tasks")]
    finally:
        conn.close()
    old_bad = [t for t in old_titles if q.is_fragment(t)]
    old_rate = (100.0 * len(old_bad) / len(old_titles)) if old_titles else 0.0
    print(f"  对照 · 旧库当时存的待办：      {len(old_titles)}，其中碎片 "
          f"{len(old_bad)}（{old_rate:.1f}%）")
    print()

    if problems:
        print(f"  --- 新解析器仍有问题的样本（最多 {args.show} 条）---")
        for item in problems[: args.show]:
            preview = " ".join(item["text"].split())[:60]
            print(f"    [{item['origin']}] 标题={item['title']!r}｜{item['why']}")
            print(f"        输入：{preview!r}")
    else:
        print("  ✓ 新解析器没有产出任何碎片标题")

    # 退出码：有问题返回 1，方便以后接进自动化
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
