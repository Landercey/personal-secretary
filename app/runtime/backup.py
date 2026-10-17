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
FULL_KEEP = 3            # 完整备份（带附件）留最近几份 —— 它可能有几百 MB


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
    for old in sorted(target_dir.glob("完整备份-*.zip"))[:-FULL_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


def attachments_summary() -> dict:
    """附件有多少个、多大。界面上要能告诉用户"备份里到底包不包括这些图"。
    """
    from .config import attachments_dir

    count = 0
    size = 0
    root = attachments_dir()
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_file():
                count += 1
                try:
                    size += path.stat().st_size
                except OSError:
                    pass
    return {"count": count, "mb": round(size / 1048576, 1)}


def full_backup() -> Path | None:
    """把**全部**数据打成一个 zip：库 + 配置 + 附件。

    为什么每天那份不够：那份只有库。图片、Word、PPT 都在 `data/attachments/`，
    换了电脑或者整个文件夹删了，光有库那些待办点开也是"文件不在了"。
    这个 zip 能直接拷走，换台机器解压就能用。

    ⚠️ 里面有 config.toml（推送地址、token 都在里面），别随手发给别人。
    """
    import zipfile

    from .config import attachments_dir

    db = default_data_dir() / "secretary.db"
    if not db.is_file():
        return None
    target_dir = folder()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"完整备份-{stamp}.zip"
    # 库先做一份一致快照（WAL 模式下直接读文件会漏掉还没落盘的部分）
    snapshot = target_dir / f".snapshot-{stamp}.db"
    try:
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dst = sqlite3.connect(snapshot)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(snapshot, "secretary.db")
            config = default_root() / "config.toml"
            if config.is_file():
                bundle.write(config, "config.toml")
            root = attachments_dir()
            if root.is_dir():
                for path in root.rglob("*"):
                    if path.is_file():
                        bundle.write(path, "attachments/" + str(path.relative_to(root)))
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        return None
    finally:
        try:
            snapshot.unlink()
        except OSError:
            pass
    _prune(target_dir)
    return target


def list_backups() -> list[dict]:
    """有哪些备份可用（新的在前）。顺手报一下每份里有多少条待办，方便挑。"""
    items: list[dict] = []
    for path in sorted(folder().glob("secretary-*.db"), reverse=True):
        stamp = path.stem.split("-", 1)[-1]
        count = None
        messages = None
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                count = int(con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
                messages = int(con.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            finally:
                con.close()
        except sqlite3.Error:
            count = None                      # 这份坏了，就别让人选它
        items.append({"name": path.name,
                      "at": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) == 8 else stamp,
                      "size_kb": max(1, path.stat().st_size // 1024),
                      "tasks": count, "messages": messages})
    return items


def find(name: str) -> Path | None:
    """按名字找一份备份。只认 `secretary-YYYYMMDD.db` 这种形状（防路径穿越）。"""
    text = str(name or "").strip()
    if not text.startswith("secretary-") or not text.endswith(".db"):
        return None
    if "/" in text or "\\" in text or ".." in text:
        return None
    path = folder() / text
    return path if path.is_file() else None


def save_before_restore() -> Path | None:
    """恢复之前先把**现在这份**另存一份 —— 恢复本身也要能反悔。"""
    db = default_data_dir() / "secretary.db"
    if not db.is_file():
        return None
    target_dir = folder()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # 前缀不用 secretary-：这样它不会被"只留 7 份"的清理规则扫到
    target = target_dir / f"before-restore-{stamp}.db"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target
