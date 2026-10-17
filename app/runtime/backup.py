"""每天自动备份一次：库 + 配置。

为什么要有：用户丢过一次配置、一次待办（2026-10-13/16）。现在库坏了只能靠作者
手动拷 —— 那等于没有备份。库只有几百 KB，拷 7 份也就几 MB，成本可以忽略。

放在 `data/backups/`，一天一份，留 7 天。用 sqlite 的 backup 接口而不是直接拷文件：
WAL 模式下直接拷 `.db` 会漏掉还没落盘的那部分。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from .config import default_data_dir, default_root

KEEP = 7                 # 留最近几份


def folder() -> Path:
    return default_data_dir() / "backups"


def run_daily(force: bool = False) -> Path | None:
    """今天还没备份就备一次。返回备份到哪儿了；今天已经备过返回 None。

    绝不抛异常：备份失败不该拦住程序启动（大不了这份没备上）。
    """
    db = default_data_dir() / "secretary.db"
    if not db.is_file():
        return None
    target_dir = folder()
    stamp = datetime.now().strftime("%Y%m%d")
    target = target_dir / f"secretary-{stamp}.db"
    try:
        if target.exists() and not force:
            return None
        target_dir.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换：界面和后台两个进程可能同时启动，
        # 谁先写完谁生效，不会留下半个库。
        handle, temp_name = tempfile.mkstemp(dir=str(target_dir), suffix=".db")
        os.close(handle)
        temp = Path(temp_name)
        # ⚠️ `with sqlite3.connect(...)` 只提交事务、**不关连接** ——
        # 不显式 close 的话临时文件一直被占着，os.replace 会 PermissionError
        # （备份就静默失败，等于没备）。
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dst = sqlite3.connect(temp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        os.replace(temp, target)
        config = default_root() / "config.toml"
        if config.is_file():
            (target_dir / f"config-{stamp}.toml").write_bytes(config.read_bytes())
        _prune(target_dir)
        return target
    except Exception:
        return None


def _prune(target_dir: Path) -> None:
    """只留最近 KEEP 份（文件名里带日期，按名字排就是按日期排）。"""
    for pattern in ("secretary-*.db", "config-*.toml"):
        files = sorted(target_dir.glob(pattern))
        for old in files[:-KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
