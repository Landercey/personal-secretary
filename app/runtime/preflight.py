"""启动自检：库有没有坏、盘还够不够、数据目录能不能写。

三样都不该拦住启动 —— 但三样都不能"什么都不说"：
  · 库坏了：把坏文件改名留档（不删），建一个新的，并告诉用户"可从备份恢复"；
  · 盘快满了：提前说，别等写不进去才报错；
  · 数据目录不能写：这就是"程序能开但什么都存不下来"的根因。

返回的都是 `{"code", "level", "message"}`：`level` 只有 warn / error，
界面直接把 message 显示出来（错误码留给日志和反馈包）。
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

MIN_FREE_MB = 200          # 低于这个数就提醒（附件、备份都要空间）


def _item(code: str, level: str, message: str) -> dict:
    return {"code": code, "level": level, "message": message}


def check(store_path: Path) -> list[dict]:
    notes: list[dict] = []
    folder = Path(store_path).parent

    # ① 数据目录能不能写（临时文件写一下再删）
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=str(folder), suffix=".probe", delete=True):
            pass
    except OSError as exc:
        notes.append(_item("PREFLIGHT.DATA.READONLY", "error",
                           f"数据目录写不进去（{exc.strerror or exc}）："
                           f"{folder}。程序能开，但什么都存不下来。"))
        return notes          # 写不进去就别再查库了，查了也没法修

    # ② 剩余空间
    try:
        free_mb = shutil.disk_usage(folder).free // 1048576
        if free_mb < MIN_FREE_MB:
            notes.append(_item("PREFLIGHT.DISK.LOW", "warn",
                               f"磁盘只剩 {free_mb} MB 了 —— 附件和备份会写不进去，"
                               "建议清一下。"))
    except OSError:
        pass

    # ③ 库完整性（坏就改名留档，让程序用一份新的起来）
    db = Path(store_path)
    if db.is_file():
        damaged = False
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = con.execute("PRAGMA integrity_check").fetchall()
                damaged = not (len(rows) == 1 and str(rows[0][0]).lower() == "ok")
            finally:
                con.close()
        except sqlite3.Error:
            damaged = True
        if damaged:
            moved = _move_aside(db)
            notes.append(_item("PREFLIGHT.DB.CORRUPT", "error",
                               f"待办库读坏了{'，旧文件已另存为 ' + moved.name if moved else ''}。"
                               "程序会用一个新的库继续跑 —— 想找回旧待办，"
                               "去 设置 → 数据保留 里选一份备份恢复。"))
    return notes


def _move_aside(db: Path) -> Path | None:
    """把坏库连同 -wal/-shm 一起改名（不删：万一还能救）。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = db.with_name(f"{db.stem}.broken-{stamp}{db.suffix}")
    try:
        db.replace(target)
    except OSError:
        return None
    for suffix in ("-wal", "-shm"):
        side = db.with_name(db.name + suffix)
        if side.exists():
            try:
                side.replace(db.with_name(target.name + suffix))
            except OSError:
                pass
    return target
