"""异常漏斗：一条错误进来，两副面孔出去。

  · 给人看的：`errors.human(编号)` —— 一句话 + 怎么办，界面/弹框直接用；
  · 给作者看的：`data/logs/events-YYYYMMDD.jsonl` —— 编号、阶段、插件、
    msg_id、耗时、**完整 traceback**，一行一条，能 grep 能进诊断包。

为什么不用 print：打包版是窗口程序，print 进的是 background.log 那口井，
用户在界面上看不到；而且没有编号，用户贴过来你还得猜是哪条路径。

留着不删旧文件：按天切，只留最近 7 天（一天几十 KB，成本可以忽略）。
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from .config import default_data_dir

KEEP_DAYS = 7
_lock = threading.Lock()

# 写日志前把明显的秘密抹掉：这条日志可能会被用户贴到群里 / 进诊断包。
_SECRET = re.compile(
    r"(?i)((?:access_token|api_key|key|token|password|webhook)\s*[=:]\s*)['\"]?[^\s'\"&]+")


def _redact(text: str) -> str:
    return _SECRET.sub(r"\1***", str(text or ""))


def folder() -> Path:
    return default_data_dir() / "logs"


def _file() -> Path:
    return folder() / f"events-{datetime.now().strftime('%Y%m%d')}.jsonl"


def note(level: str, code: str, *, stage: str = "", detail: str = "",
         exc: BaseException | None = None, **ctx) -> dict:
    """记一条。**返回给界面用的人话**（不想记的时候直接调 errors.human）。

    绝不抛异常：日志写不进去也不能把功能带崩（磁盘满的时候就是这样）。
    """
    from .errors import human as human_text

    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level": str(level or "info"),
        "code": str(code or ""),
        "stage": str(stage or ""),
        "detail": _redact(detail),
    }
    for key, value in (ctx or {}).items():
        if value in (None, ""):
            continue
        record[str(key)] = _redact(value) if isinstance(value, str) else value
    if exc is not None:
        record["exc_type"] = type(exc).__name__
        record["traceback"] = _redact(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))[-4000:]
    try:
        with _lock:
            folder().mkdir(parents=True, exist_ok=True)
            with _file().open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass                       # 写不进日志也要让调用方拿到人话
    return human_text(code, detail)


def purge_old(keep: int = KEEP_DAYS) -> None:
    """只留最近几天的 jsonl（启动时顺手清一次）。"""
    try:
        files = sorted(folder().glob("events-*.jsonl"))
        for old in files[:-keep]:
            old.unlink()
    except OSError:
        pass


def install_hooks(alert=None) -> None:
    """把"没人接的异常"也接住：主线程、后台线程、整个进程崩之前。

    `alert(title, text)` 可选：打包版没有控制台，崩了只在屏幕上闪一下就没，
    用户根本不知道发生了什么。给了就用系统弹框说一句。
    """
    def _handle(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        message = note("error", "APP.CRASH.UNHANDLED", stage="app",
                       detail=f"{exc_type.__name__}: {exc}", exc=exc)
        if alert is not None:
            try:
                alert("出了点问题", message)
            except Exception:
                pass

    sys.excepthook = _handle
    try:
        threading.excepthook = lambda args: _handle(
            args.exc_type, args.exc_value, args.exc_traceback)
    except Exception:
        pass
