"""界面是"没窗口的程序"，所以它起的子进程一个都不许弹黑框。

为什么专门写个检查：黑框一闪这件事**测试测不出来、日志里也没有**，
只能靠人眼看见。2026-10-12 一天踩了两处，根因是同一件事 ——
起进程没带 `CREATE_NO_WINDOW`（`runtime/deploy.py` 里的 `no_window_flags()`）：

  · 启动时补桌面快捷方式 → **每次打开都先闪一个黑框**（用户报告的原话：
    "启动程序会先弹出一个黑框再出现程序"）
  · 拖张图进去 OCR → 黑框要挂一秒多，比闪一下更像出事了

这个检查是静态的：把 app/ 和 run.py 里每个 `subprocess.run/Popen` 找出来，
没带 `creationflags` 就报错。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 会真的起进程的那几个名字。`subprocess.TimeoutExpired` 这类异常不算。
SPAWNERS = frozenset({"run", "Popen", "call", "check_call", "check_output"})

# 只查"界面那一份"会用到的代码。tools/ 下是开发者脚本，在终端里跑，
# 弹不弹窗口无所谓。
TARGETS = [ROOT / "run.py", *sorted((ROOT / "app").rglob("*.py"))]


def scan(path: Path) -> tuple[list[int], int]:
    """返回 (没带 creationflags 的行号, 一共找到几处起进程)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad: list[int] = []
    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in SPAWNERS):
            continue
        owner = func.value
        if not (isinstance(owner, ast.Name) and owner.id == "subprocess"):
            continue
        total += 1
        if "creationflags" not in {kw.arg for kw in node.keywords}:
            bad.append(node.lineno)
    return bad, total


class TestNoConsoleFlash(unittest.TestCase):
    def test_the_scan_actually_sees_something(self):
        """反面确认：哪天代码搬了家，别让这个检查变成"什么都没查"还绿着。"""
        found = [path for path in TARGETS if scan(path)[1]]
        self.assertGreaterEqual(
            len(found), 3, f"只扫到 {len(found)} 个文件里有子进程，检查八成失效了")

    def test_every_subprocess_is_hidden(self):
        problems: list[str] = []
        for path in TARGETS:
            for line in scan(path)[0]:
                problems.append(f"{path.relative_to(ROOT)}:{line}")
        self.assertEqual(
            problems, [],
            "这些子进程会弹黑框，补上 creationflags=no_window_flags()：\n"
            + "\n".join(problems))
