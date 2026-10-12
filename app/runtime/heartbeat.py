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
LOCK_FILE = "serve.lock"

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


# 同一个数据目录只允许一个后台进程 —— 独占锁的句柄留在这儿（活着就一直占着）。
_lock_handle = None


def claim_single_instance() -> bool:
    """抢"这个数据目录的后台"这把椅子。抢到返回 True，别人已经在跑返回 False。

    **为什么必须有**（用户报的"软件没登录却一直有推送"）：开机自启那份
    `--serve` 和手动点「启动 QQ」那份可以同时在跑 —— 界面那个按钮只拦得住
    它自己那条路，拦不住自启那条。两个后台各有一个定时器，同一批待办就可能
    被推两遍；而且它们还会抢同一个 18080 端口，第二个其实是"半死"状态：
    收不到消息、但推送照发。

    做法：Windows 的文件独占锁（`msvcrt`）。锁跟着**进程**走 ——
    进程一死（哪怕是崩的）系统自动放锁，不会留下"上次没退干净、
    这次永远起不来"的死锁文件。
    """
    global _lock_handle
    if _lock_handle is not None:
        return True                     # 本进程已经拿过了
    import msvcrt

    from .config import default_data_dir

    folder = default_data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    handle = open(folder / LOCK_FILE, "a+b")
    try:
        handle.seek(0)
        handle.write(b"1")              # 锁一个真实存在的字节，别锁到 EOF 外面
        handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return False
    _lock_handle = handle
    return True


def release_single_instance() -> None:
    """放掉那把椅子（给测试用；正常跑时进程一退系统就自动放了）。"""
    global _lock_handle
    if _lock_handle is None:
        return
    try:
        _lock_handle.close()
    except OSError:
        pass
    _lock_handle = None
