"""软件版本号 —— **只有这一个地方要改**。

为什么单独立一个文件（而不是塞在 run.py 或 config.py 里）：
它要被四个地方读 —— 界面（设置页那行小字）、`--version`、反馈包（"你是哪一版"）、
打包脚本（补丁包名字）。散在四处迟早对不上，而"版本对不上"的后果是
用户报问题时我拿一份错的代码去查。

怎么改版本号（每次发版只做这一件事）：

    0.1.0 → 0.1.1   修了几个 bug、没加新功能
    0.1.0 → 0.2.0   加了新功能（能用的东西变多了）
    0.1.0 → 1.0.0   作者认为"普通人可以放心用了"

改完把它写进 `CHANGELOG.md` 顶上一行，再跑 `python tools/build_exe.py`。
打包时这个号会写进 exe 旁边的 `build-info.txt`，**补丁包名字里也带它**，
用户一看就知道自己那份是第几版、该不该升。
"""

from __future__ import annotations

from pathlib import Path

VERSION = "4.0.7"


def version_text(root: Path | None = None) -> str:
    """给人看的一行：`v0.1.0（构建 2026-10-12 10:30｜提交 abc123）`。

    打包版旁边有 `build-info.txt`（打脚本写的），源码运行时没有 —— 那就只显示版本号。
    为什么要带上构建时间：同一个版本号可能打好几次（改了没发版），
    用户发来的那一行里有时间，我才知道是哪一次打的。
    """
    if root is None:
        from .runtime.config import default_root

        root = default_root()
    info = Path(root) / "build-info.txt"
    if info.is_file():
        try:
            built = info.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            built = ""
        if built:
            return f"v{VERSION}（构建 {built}）"
    return f"v{VERSION}"
