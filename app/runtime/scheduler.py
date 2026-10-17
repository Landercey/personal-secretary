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
  改"提醒力度有哪些档可选" → config.toml 的 [remind] options + `remind_options()`
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

除了上面那份"每天几次"的汇总，这里还负责**临期提醒**：每条待办有自己的
提醒力度（= 截止前多少分钟，在详情里选；见 models.Task），选了的那种
会在到点前单独推这一条。
为什么值得单独推：汇总是一天两次的"大扫除"，而"三点开会、现在两点半"
这种事，等到晚上九点那份汇总里才看到就晚了。
"""

from __future__ import annotations

import threading
import time
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import config

# 多久检查一次。30 秒足够精确到分钟级，又不会白耗 CPU。
DEFAULT_TICK = 30.0

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
SLOT_FORMAT = "%H:%M"

# 到点后这么多分钟内都算"该提醒了"。
# 为什么不掐到秒：调度器每 30 秒醒一次，差几秒就漏一条提醒太蠢；
# 窗口外（比如电脑那会儿关着）就跳过 —— 那条已经进过汇总了，不用补。
REMIND_WINDOW_MINUTES = 5

def human_minutes(minutes: int) -> str:
    """分钟数 → 人话（"1 天" / "1 小时" / "30 分钟"）。"""
    if minutes >= 1440 and minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


def remind_options() -> list[int]:
    """详情里那个「提醒力度」下拉能选的档（分钟），从小到大去掉重复和坏值。

    来自 config.toml 的 [remind] options。写坏的项直接丢掉 ——
    一个手滑的数字不该让整张下拉表消失。
    """
    raw = config.get("remind", "options", [])
    if isinstance(raw, str):
        raw = raw.replace("，", ",").split(",")
    found: set[int] = set()
    for item in raw or []:
        try:
            minutes = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if minutes > 0:
            found.add(minutes)
    return sorted(found)


def describe_remind(minutes: int) -> str:
    """**单个**提前量用一句话怎么说（下拉的选项文案、以及老数据的兜底显示）。"""
    minutes = int(minutes or 0)
    if minutes <= 0:
        return "默认：只在每日汇总里出现"
    return f"提前 {human_minutes(minutes)}提醒一次"


def describe_leads(leads: list[int]) -> str:
    """一条待办的提醒力度（可能好几个）用一句话怎么说 —— 待办详情里显示这句。

    用户原话："要能实现多次提醒，不是只能选一个" —— 那就得能一眼看出
    这条会在哪几个时间点提醒：**提前 1 小时、提前 10 分钟各提醒一次**。
    """
    clean = sorted({int(item) for item in leads if int(item) > 0}, reverse=True)
    if not clean:
        return "默认：只在每日汇总里出现"
    when = "、".join(f"提前 {human_minutes(minutes)}" for minutes in clean)
    return f"{when}各提醒一次"


def task_leads(task) -> list[int]:
    """这条待办要在截止前多少分钟各提醒一次（去掉重复、按从早到晚）。

    优先看 `remind_leads`（可以好几个）；老数据只有 `remind_level` 一个值。
    """
    raw = str(getattr(task, "remind_leads", "") or "")
    found: set[int] = set()
    for piece in raw.replace("，", ",").split(","):
        try:
            minutes = int(piece.strip())
        except ValueError:
            continue
        if minutes > 0:
            found.add(minutes)
    if not found:
        single = int(getattr(task, "remind_level", 0) or 0)
        if single > 0:
            found.add(single)
    return sorted(found, reverse=True)


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


def _minutes_of_day(text: str) -> int | None:
    """“23:00” → 1380。写坏的返回 None（这一项就当没配）。"""
    try:
        hour, _, minute = str(text or "").replace("：", ":").partition(":")
        value = int(hour) * 60 + int(minute or 0)
    except (TypeError, ValueError):
        return None
    return value if 0 <= value < 24 * 60 else None


def in_quiet_hours(moment: datetime, raw=None) -> bool:
    """现在是不是免打扰时段（默认 23:00–07:30，两头留空 = 关掉）。

    只压**临期提醒**，不压汇总 —— 汇总的时间是用户自己挑的，
    他挑 7:00 发早报，程序不能因为"免打扰到 7:30"把早报吞了。
    """
    window = raw if raw is not None else config.get("push", "quiet_hours", "23:00-07:30")
    text = str(window or "").strip()
    if not text or "-" not in text:
        return False
    start_text, _, end_text = text.partition("-")
    start, end = _minutes_of_day(start_text), _minutes_of_day(end_text)
    if start is None or end is None or start == end:
        return False
    now = moment.hour * 60 + moment.minute
    if start < end:                      # 同一天内，比如 12:00-13:30
        return start <= now < end
    return now >= start or now < end     # 跨午夜，比如 23:00-07:30


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


def slot_horizon(moment: datetime) -> int:
    """这一份汇总该管到第几天（0 = 只看今天，1 = 看到明天）。

    标题写着"今天要做的"，正文却把 10 月 30 号的交作业也列上 —— 用户的原话是
    "推送的太繁琐了"。早报只看今天，晚报看明天（顺带把逾期的补上）；
    再远的等它到日子自然会进来，这样每一份都短、都能一眼扫完。
    """
    return 0 if moment.hour < 12 else 1


def within_horizon(task, moment: datetime, horizon: int) -> bool:
    """这条该不该出现在这一份汇总里。

    逾期的一律要（它们还没做完）；没写时间的也算今天（和界面「今天」一个口径）。
    """
    if not task.due_at:
        return True
    try:
        due = datetime.strptime(str(task.due_at), TIME_FORMAT)
    except ValueError:
        return True                        # 时间认不出来的别藏起来
    if due < moment:                       # 逾期：一定要提醒
        return True
    today = moment.date()
    return (due.date() - today).days <= horizon


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


def due_reminders(store, now: datetime | None = None,
                  limit: int = 5) -> list[tuple[object, int]]:
    """现在该单独提醒的待办：[(待办, 提前量分钟), …]。

    判据三样都得占：有截止时间、还没完成、而且**正好走进它的提前量窗口**
    （到点后 REMIND_WINDOW_MINUTES 分钟内都算）。
    没有截止时间的只在汇总里出现 —— 没有"截止前"可言。
    提前量是这条待办自己的（`task_leads`）—— **可以有多个**：设了"提前一小时、
    提前十分钟"，那两个时间点各提醒一次（用户要的就是"多次提醒"）。
    没设的只在汇总里出现，跳过。

    limit 兜住一次推太多：一口气 50 条提醒比不提醒还烦。
    返回按提前量从小到大排（越急的越靠前）。
    """
    moment = now or datetime.now()
    found: list[tuple[object, int]] = []
    for task in store.list_tasks("open", limit=200):
        leads = task_leads(task)
        if not leads or not task.due_at:
            continue
        try:
            due = datetime.strptime(str(task.due_at), TIME_FORMAT)
        except ValueError:
            continue                      # 时间格式坏了的先不管它
        for minutes in leads:
            fire = due - timedelta(minutes=minutes)
            if fire <= moment < fire + timedelta(minutes=REMIND_WINDOW_MINUTES):
                found.append((task, minutes))
    found.sort(key=lambda item: item[1])
    return found[:max(0, int(limit))]


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

        moment = now or datetime.now()
        sent: list[str] = []
        for slot in due_slots(moment):
            if self.store.already_pushed(slot.batch):
                continue
            if self._deliver(slot):
                sent.append(slot.batch)
        # 汇总之外，再看有没有"快到点了"的（提醒强度高的那几条）
        sent.extend(self._deliver_due_reminders(moment))
        return sent

    def _blocked(self) -> tuple[bool, str]:
        """现在允不允许推送。"""
        from .sysmon import busy

        return busy()

    def _deliver(self, slot: Slot) -> bool:
        """组一份汇总发出去，并记账。"""
        from ..sinks.console import build_digest

        everything = self.store.list_tasks("open", limit=50)
        horizon = slot_horizon(slot.when)
        tasks = [t for t in everything if within_horizon(t, slot.when, horizon)]
        payload = build_digest(tasks, title=slot.title,
                               rest=len(everything) - len(tasks))
        payload.channel = getattr(self.sink, "name", "console")

        # 收消息那边没连上（没登录/后台没跑）时，才在推送尾巴上说一句。
        # 用户的原话是"我软件没登录，怎么一直有推送" —— 推送本来就跟登录无关
        # （推的是你的待办汇总），但**不写清楚就会让人以为程序在瞎推**。
        # ⚠️ 只在"出事了"的时候写：一切正常时那句状态＋数据目录是纯噪音，
        # 用户 2026-10-13 的原话是"推送的太繁琐了"。
        from .heartbeat import state as beat_state

        detail = str(beat_state(self.store).get("detail") or "")
        if detail and not detail.startswith("登录号"):
            # 带上数据目录：用户要是装了两份（比如旧的没删、还开着机自启），
            # 一眼就能看出这条汇总是**哪一份**在推 —— 用户报过
            # "界面里一条待办都没有、手机上却一直收到老待办"，根因就是这个。
            from .config import default_data_dir

            where = re.sub(r"\\Users\\[^\\]+", r"\\Users\\…",
                           str(default_data_dir()))
            payload.body += f"\n\n（⚠️ 收消息那边：{detail}；这份汇总来自：{where}）"

        result = self._send(slot.batch, payload)
        if result is None:
            return False
        if result.ok:
            self._say(f"{slot.batch} 已推送「{slot.title}」"
                      f"（{len(tasks)} 条，另有 {len(everything) - len(tasks)} 条更远）")
        else:
            self._say(f"{slot.batch} 推送失败：{result.message}")
            # 失败原因（钉钉的 errcode、企微的 45009…）要进结构化日志：
            # 界面上那句话两秒就没了，用户来问时你手上得有原始的。
            from . import logger

            logger.note("error", "PUSH.DELIVER.FAILED", stage="output",
                        detail=str(result.message), batch=slot.batch,
                        channel=getattr(self.sink, "name", ""))
        return bool(result.ok)

    def _deliver_due_reminders(self, moment: datetime) -> list[str]:
        """临期提醒：按每档配的提前量，单独推这一条。"""
        if in_quiet_hours(moment):
            # 免打扰时段：**只压临期提醒**，汇总照发（汇总时间是用户自己挑的）。
            # 半夜 2 点"还有 3 分钟"对谁都没用 —— 那条待办本来就在列表里，
            # 早上第一份汇总会带上它。
            return []
        done: list[str] = []
        for task, minutes in due_reminders(self.store, moment):
            batch = f"remind:{task.task_id}:{minutes}"
            if self.store.already_pushed(batch):
                continue          # 重启 / 手动再开一次，都不能重复推
            if self._deliver_reminder(task, minutes, batch):
                done.append(batch)
        return done

    def _deliver_reminder(self, task, minutes: int, batch: str) -> bool:
        """单独推一条"快到点了"。内容朴素：就这一件事，不夹带别的。"""
        from ..contracts.models import PushPayload

        body = [f"【{task.title}】"]
        if task.due_at:
            body.append(f"截止：{task.due_at}")
        if task.location:
            body.append(f"地点：{task.location}")
        if task.source_name:
            body.append(f"来自：{task.source_name}")
        if task.quoted:
            body.append(f"\n原话：{task.quoted}")

        payload = PushPayload(
            channel=getattr(self.sink, "name", "console"), kind="text",
            title=f"⏰ 还有 {human_minutes(minutes)}：{task.title}",
            body="\n".join(body))
        result = self._send(batch, payload)
        ok = bool(result and result.ok)
        self._say(f"{batch} 临期提醒{'已推送' if ok else '没发出去'}（{task.title}）")
        return ok

    def _send(self, batch: str, payload):
        """真正发出去 + 记一笔。汇总和临期提醒共用这一条路。

        返回 sink 的结果对象；抛异常时已经记过账，返回 None。
        """
        try:
            result = self.sink.deliver(payload)
        except Exception as exc:
            self.store.log_push(batch, payload.channel, False,
                                f"{type(exc).__name__}: {exc}")
            self._say(f"{batch} 推送异常：{type(exc).__name__}")
            return None

        self.store.log_push(batch, payload.channel, result.ok, result.message)
        return result

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
