"""系统内存读取：判断"现在能不能干活"。

=====================================================================
为什么需要它
=====================================================================
这个工具要在你**打游戏的时候继续跑**。它的原则是：
    内存吃紧时，只记待办，不做重活（OCR、AI 分析），也不推送。

否则会出现这种事：你在打游戏，程序突然拉起一张图开始 OCR，
风扇狂转两分钟，你被打死。

=====================================================================
为什么用 ctypes 而不是 psutil
=====================================================================
Windows 自己就有 `GlobalMemoryStatusEx` 这个 API，ctypes 是标准库，
调它不需要装任何东西。psutil 能用，但它是第三个依赖 ——
能少一个是一个。

想改这里的话：阈值在 config.toml 的 [resource]，不在这里。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes


class _MemoryStatusEx(ctypes.Structure):
    """Windows 的 MEMORYSTATUSEX 结构体。字段顺序不能改。"""

    _fields_ = [
        ("dwLength", ctypes.wintypes.DWORD),
        ("dwMemoryLoad", ctypes.wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _ProcessMemoryCounters(ctypes.Structure):
    """Windows 的 PROCESS_MEMORY_COUNTERS。只想拿 WorkingSetSize 那一项。"""

    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("PageFaultCount", ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def self_rss_mb() -> float | None:
    """**这个进程自己**占了多少内存（MB，物理内存工作集）。

    用户想看"这东西到底吃多少"。界面那侧分开算：界面进程自己，
    加后台进程（后台把自己的数写进心跳里，见 runtime/heartbeat.py）。

    读不到返回 None —— 显示不出来是小事，不能因为读不到就报错。
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        # ⚠️ `GetCurrentProcess()` 返回的是伪句柄 **-1**。直接把它当 Python int
        # 传给 ctypes，它会按 32 位 int 压栈、被零扩展成 0x00000000FFFFFFFF，
        # 变成一个无效句柄 —— 调用直接失败返回 0（这个坑真踩过：读出来是 None）。
        # 包成 c_void_p 才会按指针宽度传。
        handle = ctypes.c_void_p(kernel32.GetCurrentProcess())
        ok = kernel32.K32GetProcessMemoryInfo(
            handle, ctypes.byref(counters), ctypes.c_uint(counters.cb))
        if not ok:
            return None
        return round(counters.WorkingSetSize / 1024 / 1024, 1)
    except Exception:
        return None


def memory_state() -> dict[str, float] | None:
    """读一次内存：返回 {'percent': 已用百分比, 'avail_gb': 可用 GB}。

    读不到就返回 None —— **调用方必须把 None 当成"不知道"，按放行处理**。
    宁可多做一次活，也不要因为读不到内存就把所有功能停掉。
    """
    try:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return {
            "percent": float(status.dwMemoryLoad),
            "avail_gb": status.ullAvailPhys / (1024 ** 3),
        }
    except Exception:
        return None


def busy() -> tuple[bool, str]:
    """现在算不算"内存吃紧"。返回 (是否吃紧, 人话原因)。

    阈值在 config.toml 的 [resource] mem_critical_percent（默认 88%）。
    想改成"打游戏时一条都别推"，把阈值调低；想彻底关掉这个闸门，调成 100。
    """
    from .config import config

    try:
        raw = config.get("resource", "mem_critical_percent", 88)
        # ⚠️ 不能写 `float(raw or 88)` —— 用户把阈值设成 **0** 时
        # `0 or 88` 会变成 88，配的值被静默吃掉。
        # "0 是合法值"和"没配置"必须分开判断，这是很经典的坑。
        limit = 88.0 if raw is None or str(raw).strip() == "" else float(raw)
    except (TypeError, ValueError):
        limit = 88.0
    state = memory_state()
    if state is None:
        return False, ""            # 读不到就放行，按"不知道"处理
    if state["percent"] >= limit:
        return True, (f"内存已用 {state['percent']:.0f}%"
                      f"（超过阈值 {limit:.0f}%，只记待办不干活）")
    return False, ""


def describe() -> str:
    """一句话现状，给自检显示。"""
    from .config import config

    state = memory_state()
    if state is None:
        return "内存：读不到"
    limit = config.get("resource", "mem_critical_percent", 88)
    busy_now, _reason = busy()
    return (f"内存：已用 {state['percent']:.0f}%（阈值 {limit}%）"
            f"｜可用 {state['avail_gb']:.1f} GB"
            f"{'｜当前吃紧，暂停分析推送' if busy_now else ''}")
