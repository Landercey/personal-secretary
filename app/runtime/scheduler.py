"""定时调度：到点把待办汇总推到你手机上。

=====================================================================
它在整个程序里的位置
=====================================================================
     run.py 装好零件
        ↓
     Scheduler 每隔 30 秒醒一次，问一句"现在该推了吗"
        ↓ 该推
     从 Store 取待办 → 拼成一份汇总 → 交给 Sink 发出去
        ↓
     往 push_records 记一笔（防止重启后重复推）

它只做"什么时候推"，不关心"推给谁"（那是 Sink 的事），
也不关心"推什么内容"（那是 sinks/console.py 的 build_digest）。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  改推送时间            → config.toml 的 [push] times
  改每份汇总的标题和内容 → `slot_title()` 和 sinks/console.py 的 build_digest()
  改"错过了还补推吗"     → config.toml 的 [push] catch_up_minutes
  改检查频率            → 本文件的 DEFAULT_TICK
  改"什么时候不许推"     → `_blocked()`（现在只看内存）

=====================================================================
三个设计决定，都是有理由的
=====================================================================
1. **同一批次只推一次**（记在 push_records 里）。
   程序重启、手动再开一次，都不能把早上那份早报又推一遍 ——
   手机响两遍比不推还烦。

2. **错过的可以补推，但有个窗口**（默认 120 分钟）。
   早上 7:30 关机、9:00 才开机 —— 这份早报还值得推。
   晚上 10 点才开机，再推"今天的早报"就是纯打扰，不推。

3. **内存吃紧时一条都不推**。
   你在打游戏，程序突然弹一条推送把游戏弹到后台 —— 这是最招人烦的。
   宁可晚点推。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import config

# 多久检查一次。30 秒足够精确到分钟级，又不会白耗 CPU。
DEFAULT_TICK = 30.0

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
SLOT_FORMAT = "%H:%M"


@dataclass(slots=True)
class Slot:
    """一个推送时间点。"""

    when: datetime          # 今天这个时间点的实际时刻
    label: str              # "07:30"
    batch: str              # "2026-10-08 07:30"，用来防重复
    title: str              # 推出去时那份汇总的标题


def parse_times(raw) -> list[str]:
    """把配置里的时间列表规范化成 ["07:30", "21:30"]。

    容忍用户的写法：["7:30"] 会补成 "07:30"；写坏的项直接丢掉
    （解析不了的配置不能让调度器整个不工作）。
    """
    if isinstance(raw, str):
        raw = raw.replace("，", ",").split(",")
    out: list[str] = []
    for item in raw or []:
        text = str(item).strip()
        if not text:
            continue
        try:
            hour_text, _, minute_text = text.replace("：", ":").partition(":")
            hour, minute = int(hour_text), int(minute_text or 0)
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            out.append(f"{hour:02d}:{minute:02d}")
    return sorted(set(out))


def slot_title(moment: datetime) -> str:
    """按时间点决定这份汇总叫什么。

    不额外加配置项 —— 用户只要写时间，标题自己推出来：
      · 中午以前  → 今天要做的
      · 傍晚以后  → 明天要留意的
      · 其余      → 待办汇总
    """
    if moment.hour < 12:
        return "今天要做的"
    if moment.hour >= 18:
        return "明天要留意的"
    return "待办汇总"


def due_slots(now: datetime | None = None, *, times: list[str] | None = None,
              catch_up_minutes: int | None = None) -> list[Slot]:
    """现在有哪些时间点该推了。**不查数据库**，纯计算，方便测试。

    判定：今天这个时间点已经过了，但没超过补推窗口。
    """
    moment = now or datetime.now()
    planned = parse_times(times if times is not None else
                          config.get("push", "times", ["07:30", "21:30"]))
    if catch_up_minutes is None:
        try:
            catch_up_minutes = int(config.get("push", "catch_up_minutes", 120) or 0)
        except (TypeError, ValueError):
            catch_up_minutes = 120

    found: list[Slot] = []
    for label in planned:
        hour, minute = (int(part) for part in label.split(":"))
        when = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when > moment:
            continue                       # 还没到点
        if (moment - when) > timedelta(minutes=max(0, catch_up_minutes)):
            continue                       # 错过太久，别补了
        found.append(Slot(when=when, label=label,
                          batch=f"{when.strftime('%Y-%m-%d')} {label}",
                          title=slot_title(when)))
    return found


class Scheduler:
    """定时推送。跑在后台线程里，不阻塞别的事。"""

    def __init__(self, store, sink, on_event=None, tick: float = DEFAULT_TICK) -> None:
        self.store = store
        self.sink = sink
        # on_event(text) 用来在界面上打一行日志；没有也不影响功能
        self.on_event = on_event
        self.tick = float(tick)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 开关
    def enabled(self) -> bool:
        """[push] enabled 关掉就整个不推。默认开 —— 不推的话这工具就没用了。"""
        return bool(config.get("push", "enabled", True))

    def _say(self, text: str) -> None:
        if self.on_event is not None:
            try:
                self.on_event(text)
            except Exception:
                pass

    # ------------------------------------------------------------------ 一次
    def run_once(self, now: datetime | None = None) -> list[str]:
        """检查一遍，该推就推。返回这次推了哪几个批次的编号。

        拆成独立方法是为了能在测试和 `--check-push` 里手动调 ——
        不用真的等到早上七点半。
        """
        if not self.enabled():
            return []

        blocked, reason = self._blocked()
        if blocked:
            self._say(f"暂停推送：{reason}")
            return []

        sent: list[str] = []
        for slot in due_slots(now):
            if self.store.already_pushed(slot.batch):
                continue
            if self._deliver(slot):
                sent.append(slot.batch)
        return sent

    def _blocked(self) -> tuple[bool, str]:
        """现在允不允许推送。"""
        from .sysmon import busy

        return busy()

    def _deliver(self, slot: Slot) -> bool:
        """组一份汇总发出去，并记账。"""
        from ..sinks.console import build_digest

        tasks = self.store.list_tasks("open", limit=50)
        payload = build_digest(tasks, title=slot.title)
        payload.channel = getattr(self.sink, "name", "console")

        # 内存吃紧时已经在 run_once 里挡过了，这里只做真正的发送
        try:
            result = self.sink.deliver(payload)
        except Exception as exc:
            self.store.log_push(slot.batch, payload.channel, False,
                                f"{type(exc).__name__}: {exc}")
            self._say(f"{slot.batch} 推送异常：{type(exc).__name__}")
            return False

        self.store.log_push(slot.batch, payload.channel, result.ok, result.message)
        if result.ok:
            self._say(f"{slot.batch} 已推送「{slot.title}」（{len(tasks)} 条待办）")
        else:
            self._say(f"{slot.batch} 推送失败：{result.message}")
        return bool(result.ok)

    # ------------------------------------------------------------------ 循环
    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                # 调度器自己崩了不该让整个程序停摆
                self._say(f"调度异常：{type(exc).__name__}: {exc}")
            self._stop.wait(self.tick)

    # ------------------------------------------------------------------ 展示
    def describe(self) -> str:
        """给 `--check-push` 看：配了哪几个时间点、下次是什么时候。"""
        planned = parse_times(config.get("push", "times", ["07:30", "21:30"]))
        if not planned:
            return "定时推送：没有配置任何时间点"
        now = datetime.now()
        upcoming = [label for label in planned
                    if now.replace(hour=int(label[:2]), minute=int(label[3:]),
                                   second=0, microsecond=0) > now]
        nxt = upcoming[0] if upcoming else f"明天 {planned[0]}"
        return (f"定时推送：{'已启用' if self.enabled() else '已关闭'}｜"
                f"每天 {len(planned)} 次（{'、'.join(planned)}）｜下次 {nxt}")
