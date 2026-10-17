"""启动计数 + 安全模式。

为什么要有：用户报过"更新完打不开" —— 一旦界面起不来，他连改设置的入口都没有，
只能来骂你。这里的规矩是：**连续 3 次没走到"能用了"那一步，就自动进安全模式**
（不加载插件、照常用配置），至少让他进得去。

计数写在 `data/startup.json`：启动时 +1，真正能用了就删掉这个文件。
进程崩了不会删 → 下次启动继续累加。不碰数据库（库坏的时候也要能进）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .config import default_data_dir

LIMIT = 3          # 连续几次没起来就进安全模式


def _file() -> Path:
    return default_data_dir() / "startup.json"


def note_attempt() -> int:
    """记一次启动尝试，返回"这是连续第几次"。"""
    data: dict = {}
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    count = int(data.get("attempts") or 0) + 1
    try:
        _file().parent.mkdir(parents=True, exist_ok=True)
        _file().write_text(json.dumps(
            {"attempts": count, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return count


def mark_ready() -> None:
    """走到"能用了"这一步：计数清零。

    什么时候算能用了：界面窗口建出来、或者后台接收端已经在监听。
    再往后崩（比如打游戏时被杀）不该算成"打不开"。
    """
    try:
        _file().unlink()
    except OSError:
        pass


def reset() -> None:
    """清掉计数（安全模式用：它只救一次，下次照常启动）。"""
    mark_ready()


def crash_count() -> int:
    try:
        return int(json.loads(_file().read_text(encoding="utf-8")).get("attempts") or 0)
    except (OSError, ValueError, TypeError):
        return 0
