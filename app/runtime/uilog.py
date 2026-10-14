"""界面侧的动作日志 —— 谁卡住了要看得出来。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  哪个界面调用要记一笔    → 方法上加 `@tracked`（见 app/ui/bridge.py 的 tracked）
  改"多久算卡住"          → `THRESHOLD_SECONDS`（现在 20 秒）
  改看门狗多久巡一次      → `WATCH_EVERY_SECONDS`
  日志文件在哪            → `path()`（data/ui.log），它跟着"意见反馈"一起带走
  加新的记录点            → `start()` / `finish()` 成对调用

=====================================================================
为什么要有这个（2026-10-13 真踩过）
=====================================================================
作者的窗口"未响应"。我从外面查了一圈：没有对话框、没在联网、没在算、
数据库也没被别人占 —— **一点线索都没有**，因为界面这一侧压根不写日志
（只有后台进程写 `background.log`）。只能硬关掉，然后猜。

做法很土但管用：

  · 每个界面调用进来记一行"开始"，出去记一行"完了，用了多少毫秒"；
  · 再有个**看门狗线程**：某个调用超过 `THRESHOLD_SECONDS` 还没回来，
    就往同一个文件里写"⚠️ 卡住了：xxx 已经跑了 N 秒"。

窗口冻着的时候，**看门狗线程照样在跑** —— 所以下次一卡，日志里直接写着
是哪个调用、卡了多久。这比"未响应"三个字有用得多。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

# 超过这么多秒没回来就记一条"卡住了"。20 秒是看着界面已经明显不对的档位 ——
# 正常的调用都在毫秒级，唯一接近它的是联网（各有自己的超时）。
THRESHOLD_SECONDS = 20.0
WATCH_EVERY_SECONDS = 5.0
MAX_KB = 200                       # 日志封顶，别长成一个 G

_lock = threading.Lock()
_current: dict[str, float] = {}    # {调用名: 开始时刻}；同一时刻通常只有一个
_watchdog: threading.Thread | None = None
_warned: float = 0.0


def path() -> Path:
    from .config import default_data_dir

    return default_data_dir() / "ui.log"


def _write(line: str) -> None:
    target = path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.stat().st_size > MAX_KB * 1024:
            target.write_text("", encoding="utf-8")     # 满了就重来，不做滚动
        with _lock, target.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass                        # 日志写不进去绝不能把功能带崩


def start(name: str) -> None:
    _current[name] = time.monotonic()


def finish(name: str, error: str = "") -> None:
    began = _current.pop(name, None)
    if began is None:
        return
    spent = (time.monotonic() - began) * 1000
    if error:
        _write(f"✗ {name} 出错（{spent:.0f} ms）：{error}")
    elif spent >= 1000:
        _write(f"· {name} 用了 {spent:.0f} ms")


def watch(threshold: float = THRESHOLD_SECONDS,
          every: float = WATCH_EVERY_SECONDS) -> None:
    """起看门狗线程（只起一次）。界面卡住时，靠它把现场写下来。"""
    global _watchdog, _warned
    if _watchdog is not None and _watchdog.is_alive():
        return
    _warned = 0.0

    def loop() -> None:
        global _warned
        while True:
            time.sleep(every)
            now = time.monotonic()
            for name, began in list(_current.items()):
                spent = now - began
                if spent < threshold:
                    continue
                # 每 10 秒最多记一条，别把文件刷满
                if now - _warned < 10.0:
                    continue
                _warned = now
                _write(f"⚠️ 卡住了：{name} 已经跑了 {spent:.0f} 秒还没回来")

    _watchdog = threading.Thread(target=loop, name="ui-log-watchdog", daemon=True)
    _watchdog.start()


def tail(lines: int = 20) -> list[str]:
    """最近的几行，给「意见反馈」用 —— 卡过的话它就带着现场。"""
    try:
        text = path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [ln for ln in text.splitlines() if ln.strip()][-lines:]
