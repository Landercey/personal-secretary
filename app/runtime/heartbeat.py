"""后台进程的心跳与停止信号：让界面知道它活着，也能让它停下来。

=====================================================================
为什么必须有这个
=====================================================================
界面和收消息是**两个独立进程**（这是有意的：界面关掉不该影响后台收消息）。
代价是界面**天然不知道**对面还在不在。

以前界面左下角那行"11 个群"其实是 `len(config["qq"]["group_ids"])` ——
那是**配置**，不是状态。收消息的进程没起来、或者 NapCat 掉线了，
界面照样显示得一切正常。用户看到的现象就是"我发了消息怎么没反应"，
而且一点线索都没有。

做法：收消息的循环每隔一会儿往库里记一笔"我还活着"，
界面读这一笔，看它有多新。跨进程、不依赖网络、不需要额外端口。

=====================================================================
停止信号为什么是"写个文件"而不是"杀进程"
=====================================================================
界面上的「停止后台」如果拿 PID 去杀，得先确认那个 PID 确实是我们的
后台进程 —— 查错了就是误杀别人的程序。风险大、还要 WMI。

写一个 `data/stop.request` 文件，让后台循环自己看到、自己退：
简单、安全、不依赖平台。代价是善后要记得**清掉这个文件**
（见 `clear_stop`：不清的话下次开机自启一启动就立刻退出，这个坑很典型）。

=====================================================================
想改这里的话
=====================================================================
  觉得误报"没在跑"      → 调大 STALE_SECONDS
  加别的常驻进程的心跳   → 直接 beat(store, "scheduler")，key 不一样就行
"""

from __future__ import annotations

import json
import time
from datetime import datetime

from ..contracts.models import TIME_FORMAT

KEY = "receiver_beat"
STOP_FILE = "stop.request"

# 超过这么多秒没有心跳，就认为它没在跑。
# 60 秒是"收消息的循环每 10 秒写一次"的 6 倍余量 —— 机器卡一下也不至于误报。
STALE_SECONDS = 60.0

# 多久真的往库里写一次。调用方可以每个循环都喊 beat()，这里自己节流，
# 免得 2 秒一次地把磁盘吵醒。
WRITE_EVERY_SECONDS = 10.0

_last_write = 0.0


def beat(store, detail: str = "", *, force: bool = False) -> None:
    """喊一声"我还活着"。调用方可以无脑每轮都调，这里会自己节流。"""
    global _last_write
    now = time.monotonic()
    if not force and now - _last_write < WRITE_EVERY_SECONDS:
        return
    _last_write = now
    from .sysmon import self_rss_mb

    payload = {"at": datetime.now().strftime(TIME_FORMAT), "detail": str(detail or ""),
               # 后台自己占多少内存。界面那侧要把它和"界面自己"加起来给用户看，
               # 但界面读不到别的进程的数 —— 让它自己报最省事。
               "rss_mb": self_rss_mb()}
    try:
        store.set_meta(KEY, json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass          # 心跳写不进去不该把收消息搞崩


def state(store) -> dict:
    """读心跳。界面拿它决定显示"接收中"还是"没在跑"。"""
    if store is None:
        return {"alive": False, "age_seconds": None, "detail": "", "at": "", "rss_mb": None}
    try:
        payload = json.loads(store.get_meta(KEY, "") or "{}")
    except (ValueError, TypeError):
        payload = {}
    at = str(payload.get("at") or "")
    if not at:
        return {"alive": False, "age_seconds": None, "detail": "", "at": "", "rss_mb": None}
    try:
        age = (datetime.now() - datetime.strptime(at, TIME_FORMAT)).total_seconds()
    except ValueError:
        return {"alive": False, "age_seconds": None, "detail": "", "at": at, "rss_mb": None}
    return {"alive": age < STALE_SECONDS, "age_seconds": round(age),
            "detail": str(payload.get("detail") or ""), "at": at,
            "rss_mb": payload.get("rss_mb")}


def request_stop() -> None:
    """请后台进程停下来。界面上那个「停止后台」按钮调它。"""
    from .config import default_data_dir

    folder = default_data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / STOP_FILE).write_text("stop", encoding="utf-8")


def stop_requested() -> bool:
    from .config import default_data_dir

    return (default_data_dir() / STOP_FILE).exists()


def clear_stop() -> None:
    """后台进程**启动时**先清掉上一次留下的停止请求。

    不清的话：用户点了「停止后台」→ 下次开机自启起来的进程一睁眼看到
    那个旧文件，立刻又退出了。用户会说"开机自启根本没用"。
    """
    from .config import default_data_dir

    try:
        (default_data_dir() / STOP_FILE).unlink()
    except OSError:
        pass
