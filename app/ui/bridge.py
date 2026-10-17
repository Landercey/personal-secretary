"""Python 和界面之间的桥。

网页那边用 `window.pywebview.api.xxx()` 调这里的方法，返回 JSON 能装的东西。
**界面不直接碰数据库** —— 数据一律走这里，规则留在上层。

=====================================================================
想改这里的话
=====================================================================
  界面要多显示一个字段   → 在 `_task_dict()` 里加上
  加一个新操作           → 写一个方法，然后在 index.html 里调它
  改列表分组             → Store.list_tasks() 的 view 参数
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path

from ..contracts.models import STATUS_DONE, STATUS_DROPPED, STATUS_OPEN
from ..runtime.humanize import human_due
from ..runtime.scheduler import (describe_leads, describe_remind, remind_options,
                                 task_leads)
from ..sinks.router import channel_status, push_help_section, push_ready


JUNK_LIST_MAX = 500
"""废件箱清单两边各取多少条。

以前是 100 —— 用户库里"被拦下的原文"有 374 条，于是更早的 274 条**在界面里
根本翻不到**（他报的是"费件箱有107，他只显示7"，107 = 7 条误判 + 100 条原文）。
500 够装下正常用量；真堆到 500 以上说明该清理了（保留策略在 [store] 里）。
"""


def tracked(fn):
    """把一次界面调用记进 `data/ui.log`（开始/耗时/出错）。

    为什么只挑一部分方法加：**这些是会跟外面打交道、或者可能很慢的**
    （联网、起进程、跑管线）。窗口卡住时，看门狗会把"是哪个调用卡了多久"
    写进同一个文件 —— 2026-10-13 卡死那次，就是因为没有这个，
    外面查了半天只能硬关掉重开。
    """
    import functools

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        from ..runtime import uilog

        uilog.start(fn.__name__)
        try:
            result = fn(self, *args, **kwargs)
        except Exception as exc:
            uilog.finish(fn.__name__, f"{type(exc).__name__}: {exc}")
            raise
        uilog.finish(fn.__name__)
        return result

    return wrapper


def _normalize_due(text: str) -> str:
    """把界面上的日期时间值变成库里统一的写法。

    浏览器 `<input type="datetime-local">` 给的是 "2026-10-12T09:00"，
    库里统一用 "YYYY-MM-DD HH:MM:SS"。只给日期（"2026-10-12"）时按当天
    最后一分钟算 —— 跟解析器"只说了哪天没说几点"的约定保持一致。
    """
    raw = str(text or "").strip().replace("T", " ")
    if not raw:
        return ""
    if len(raw) == 16:
        return raw + ":00"
    if len(raw) == 10:
        return raw + " 23:59:00"
    return raw


def _background_log() -> Path:
    """后台进程的输出写到哪。

    为什么要留档（真踩过）：以前 Popen 的时候把 stdout 接到了 DEVNULL ——
    后台一启动就报错退出，用户点完「启动 QQ」什么都看不到，
    只能看着界面干瞪眼。现在写进这个文件，界面在它没跑起来时把尾巴显示出来。
    """
    from ..runtime.config import default_data_dir

    return default_data_dir() / "background.log"


def _plugin_root() -> Path:
    """插件文件夹挂在哪个根目录下。

    单独抽成一个函数是为了**测试能替换它** —— 不抽的话，
    装插件的测试会往真的 `plugins/` 里写文件，把作者自己的插件弄脏。
    """
    from ..runtime.config import default_root

    return default_root()


def _tail(path: Path, lines: int = 12) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()][-lines:]


def _drop_name(token: str) -> str:
    """暂存文件名 → **用户原本的文件名**。

    暂存时为了防重名，我们在前面加了"时间戳-指纹-"。
    装插件的时候必须把这截去掉 —— 不然装进去的插件会叫
    `1700000000-caf5bab41b-mine.py`，模块名也跟着变丑。
    """
    return token.split("-", 2)[2] if token.count("-") >= 2 else token


# 一眼能看出"这是谁"的东西：长号码（QQ 号、群号）、Webhook 里的 key。
_DIGITS = re.compile(r"\d{6,}")
_SECRET = re.compile(r"(key=|access_token=|token[\"'=:\s]+)\S+", re.IGNORECASE)

_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".bmp": "image/bmp", ".webp": "image/webp", ".gif": "image/gif",
}
_PREVIEW_MAX_BYTES = 3 * 1024 * 1024      # 界面预览上限，原图照旧点「打开」看


def _preview_of(target: Path) -> str:
    """图片 → 界面能直接显示的一段（data URL）。不是图片 / 太大 / 读不了 → 空串。"""
    mime = _IMAGE_MIME.get(target.suffix.lower())
    if not mime:
        return ""
    try:
        if target.stat().st_size > _PREVIEW_MAX_BYTES:
            return ""
        blob = base64.b64encode(target.read_bytes()).decode("ascii")
    except OSError:
        return ""
    return f"data:{mime};base64,{blob}"


def _mask_user(path: str) -> str:
    """路径里的 Windows 用户名打掉（C:\\Users\\张三\\… → C:\\Users\\…）。

    反馈会被贴到群里 / Issue 上，而 Windows 用户名经常就是真名。
    只动 `\\Users\\` 后面那一段，盘符和后面几层保留 —— 排查"装了两份"
    "从临时目录跑"靠的正是这几层。
    """
    return re.sub(r"\\Users\\[^\\]+", r"\\Users\\…", str(path or ""))


def _redact(text: str) -> str:
    """把反馈文本里的号码和密钥打掉。

    **脱敏必须是默认行为，不能是选项**：用户点「复制」的时候不会想到
    "这段话里有没有我的号"。而反馈多半是要贴到群里或者 GitHub Issue 的，
    贴出去就收不回来了。

    长号码保留后 4 位：用户自己还认得出来是哪个号，别人认不出。
    """
    out = _SECRET.sub(lambda m: m.group(1) + "***", str(text or ""))
    return _DIGITS.sub(lambda m: "*" * (len(m.group()) - 4) + m.group()[-4:], out)


def _ai_ready(config) -> bool:
    """AI 增强真的能用吗（不只是"开关打开了"）。

    四条都要满足，缺一条插件就会安静地跳过 —— 所以界面上必须能看出是哪条缺了。
    """
    return not _ai_why(config)


def _ai_why(config) -> str:
    """AI 用不了时缺的是什么（能用就返回空串，界面据此决定显示哪一行）。"""
    if not config.get("ai", "enabled"):
        return ""
    if not config.get("ai", "allow_upload"):
        return "没允许把内容发出去"
    if not str(config.get("ai", "base_url", "") or "").strip():
        return "没填接口地址"
    if not str(config.get("ai", "model", "") or "").strip():
        return "没填模型名"
    return ""


class Api:
    """网页能调到的全部方法。加方法不用改别的，网页那边直接调。"""

    def __init__(self, store, plugins=(), actions=None, notices=None) -> None:
        self.store = store
        # 插件加载结果由 run.py 装配时递进来。界面不自己扫文件夹 ——
        # 扫一遍等于把插件又 import 一次，副作用会来两遍。
        self._plugins = list(plugins)
        # 要用到理解层的活（手动录入、拉课表）也由 run.py 装好再递进来。
        # 界面不自己 import 那些模块：全项目只有 run.py 一个装配点。
        self._actions = dict(actions or {})
        # 启动自检发现的问题（库坏了/盘满了/进了安全模式）——
        # 由 run.py 在启动时算好递进来，界面当提示条显示。
        self._notices = list(notices or [])
        # 网页轮询用：每次返回一个自增号，界面上"数据变了没"靠它判断
        self._revision = 0
        # 托盘图标：界面每 5 秒调一次 tray()，第一次建、之后只更新提示文字。
        # 建在 GUI 线程上（js_api 调用就跑在那儿）—— NotifyIcon 必须这样建。
        self._tray = None
        self._tray_note = ""
        # 看门狗：哪个界面调用卡住了，把它写进 data/ui.log（见 runtime/uilog.py）。
        # 窗口冻住的时候，这个线程照样在跑 —— 就是靠它下次能查。
        from ..runtime import uilog

        uilog.watch()
        self._start_update_check()

    def _start_update_check(self) -> None:
        """后台问一次"有没有新版本"。

        为什么开线程：这是网络请求，放在界面线程里会把窗口卡住几秒 ——
        用户的第一印象就毁了。界面那边只读缓存文件，**永远不等待网络**。
        默认 24 小时最多问一次（[ui] update_hours），关掉是 [ui] update_check。
        """
        import threading

        from ..runtime import update
        from ..runtime.config import config

        if not config.get("ui", "update_check", True):
            return
        hours = float(config.get("ui", "update_hours", 24) or 0)
        if not update.need_check(hours):
            return
        repo = str(config.get("ui", "update_source", "") or "").strip()
        if not repo:
            return
        threading.Thread(target=update.check_now, args=(repo,), daemon=True,
                         name="update-check").start()

    def _act(self, name: str, *args):
        handler = self._actions.get(name)
        if handler is None:
            raise RuntimeError(f"「{name}」没接上，重启一下程序就好了")
        return handler(*args)

    # ------------------------------------------------------------------ 读
    def tasks(self, view: str = "open") -> dict:
        """一个分组下的待办。

        view: open / today / tomorrow / **week（本周一~周日）** /
              **next_week（下周一~周日）** / overdue / **done（已完成）** /
              archive / junk
        """
        rows = self.store.list_tasks(view, limit=500)
        self._revision += 1
        items = [self._task_dict(t) for t in rows]
        # 「时间撞车」：同一天同一分钟有好几条 —— 多半是两件事排到一起了。
        # 在这里算而不是每条查一次库：500 条 × 1 次查询会让列表卡住。
        seen: dict[str, int] = {}
        for item in items:
            key = item["due_raw"]
            if key:
                seen[key] = seen.get(key, 0) + 1
        for item in items:
            item["conflict"] = bool(item["due_raw"]) and seen[item["due_raw"]] > 1
        return {"items": items, "revision": self._revision}

    @staticmethod
    def _task_dict(task) -> dict:
        """一条待办 → 界面要的形状。列表、搜索、废件箱共用一个。"""
        due_text, urgency = human_due(task.due_at)
        return {
            "id": task.task_id,
            "title": task.title,
            "due": due_text,
            # 时间给界面编辑用（datetime-local 要 "YYYY-MM-DDTHH:MM"）
            "due_raw": (task.due_at or "")[:16].replace(" ", "T"),
            "urgency": urgency,
            "location": task.location or "",
            "source": task.source_name or "",
            "sender": task.sender or "",
            "quoted": task.quoted or "",
            "detail": task.detail or "",
            # 这条待办选了哪几个提前量（**可以有多个**）。界面拿它勾勾选框。
            "remind": task_leads(task),
            # 这句话是给人看的（"提前 1 小时、提前 10 分钟各提醒一次" /
            # "默认：只在每日汇总里出现"）。让后端算，是为了和调度器同一套口径。
            "remind_text": describe_leads(task_leads(task)),
            # 详情里能勾的那几档（设置里配的）。**一个都不勾 = 默认**：
            # 只在每日汇总里出现 —— 所以这里不放那个"默认"项，它不是一个提前量。
            # 文案由后端一起给（和调度器同一套口径），界面不自己拼"1 小时"这种词。
            "remind_options": [{"minutes": m, "label": describe_remind(m)}
                               for m in remind_options()],
            "attachments": len(task.attachments),
            "files": [{"name": a.name, "path": a.path, "size": int(a.size),
                       "kind": a.kind} for a in task.attachments],
            "status": task.status,
            "done": task.status == STATUS_DONE,
            "archived": bool(task.is_archived),
            # 后台 AI 正在精修这条（规则版先入库，AI 回来再改写标题）
            "ai": bool(getattr(task, "ai_pending", False)),
        }

    def search(self, keyword: str, view: str = "all") -> dict:
        """在当前分组里找。

        **只在当前分组里**（用户报的问题）：在「今天」里搜，已完成/已归档/
        下个月的全冒出来了。搜索框就在这个分组的标题旁边，预期是"筛这个列表"。
        """
        rows = self.store.search(keyword, view=view or "all", limit=200)
        # 顺手把**原始消息**也搜一遍：用户记得的往往是原话
        # （"班长说的截止几号"），提炼后的标题不一定留着那个数字。
        # 原文搜索结果单独一组，不混进待办列表。
        found = self.store.search_messages(keyword, limit=20)
        return {"items": [self._task_dict(t) for t in rows],
                "messages": [
                    {"at": item["at"], "source": item["source"],
                     "kind": item["kind"], "sender": item["sender"],
                     "text": " ".join(str(item["text"] or "").split())[:160]}
                    for item in found],
                "keyword": str(keyword or ""), "view": view or "all"}

    @tracked
    def backups(self) -> dict:
        """有哪些自动备份可用（设置 → 数据保留 里那个下拉框）。"""
        from ..runtime import backup

        return {"items": backup.list_backups(),
                # 附件不在每天的库里（在 data/attachments/）—— 摆出来，
                # 用户才知道"光恢复库、图还在不在"。
                "attachments": backup.attachments_summary()}

    @tracked
    def full_backup(self) -> dict:
        """导出一份完整备份（库 + 配置 + 附件）。返回 zip 在哪。"""
        from ..runtime import backup

        path = backup.full_backup()
        if path is None:
            return {"ok": False, "message": "没能导出（库读不到？看看磁盘还剩多少）"}
        mb = max(1, round(path.stat().st_size / 1048576))
        return {"ok": True, "path": str(path), "mb": mb,
                "message": f"完整备份打好了（{mb} MB）"}

    @tracked
    def restore_backup(self, name: str) -> dict:
        """用一份自动备份覆盖现在的库。

        恢复前**先把现在这份另存**（`before-restore-*.db`）—— 恢复本身也要能反悔。
        只恢复数据库（待办/消息/废件箱/推送记录），**不动 config.toml**：
        用户的密钥、群号、推送时间这些是"现在的配置"，拿旧备份盖掉才是帮倒忙。
        """
        from ..runtime import backup

        path = backup.find(name)
        if path is None:
            return {"ok": False, "message": "没有这份备份（可能已经被清理了）"}
        keep = backup.save_before_restore()
        try:
            self.store.restore_from(path)
        except Exception as exc:
            return {"ok": False, "message": f"恢复失败：{type(exc).__name__}: {exc}"}
        self._revision += 1
        where = f"，恢复前那份另存为 {keep.name}" if keep else ""
        return {"ok": True, "message": f"已恢复到 {name}{where}。列表已经刷新。"}

    @tracked
    def bulk(self, action: str, ids) -> dict:
        """批量操作：完成 / 归档 / 判误判。

        为什么要它：用户 2026-10-17 时列表里 82 条未完成、40 条已完成，
        一条条点太慢。`archive_done` 是"把已完成的全部归档"。
        """
        if action == "archive_done":
            count = self.store.archive_all_done()
            self._last_action = None            # 这一下没法一条条撤销，别给假希望
            return {"ok": True, "count": count}
        clean = [int(i) for i in (ids or []) if str(i).isdigit()]
        if not clean:
            return {"ok": False, "message": "没有选中任何一条"}
        for task_id in clean:
            if action == "done":
                self.store.set_status(task_id, "done")
            elif action == "archive":
                self.store.archive(task_id, "批量归档")
            elif action == "drop":
                self.store.set_status(task_id, "dropped", "批量判成误判")
            else:
                return {"ok": False, "message": f"不认识的操作：{action}"}
        self._remember(action, clean)
        self._revision += 1
        return {"ok": True, "count": len(clean)}

    def counts(self) -> dict:
        """左侧导航上的数字。"""
        return {
            "open": self.store.count_tasks("open"),
            "today": self.store.count_tasks("today"),
            "tomorrow": self.store.count_tasks("tomorrow"),
            "week": self.store.count_tasks("week"),
            "next_week": self.store.count_tasks("next_week"),
            "overdue": self.store.count_tasks("overdue"),
            "done": self.store.count_tasks("done"),
            "archive": self.store.count_tasks("archive"),
            # 废件箱这个数字**必须等于清单里有几条**。清单是"你标成误判的待办"
            # + "被规则拦下的原文"两样合起来的，只算前者就会写成 7、里面躺着 107
            # （用户 2026-10-16："费件箱有107，他只显示7"）。两样相加才是真的。
            "junk": self.store.count_tasks("junk") + self.store.count_dropped(),
        }

    @tracked
    def today_stats(self) -> dict:
        """今天的战报：收到多少、收下多少、拦下多少。界面拿它证明"过滤真的在干活"。"""
        return self.store.today_stats()

    @tracked
    def total_stats(self) -> dict:
        """装机以来的累计数 —— 回答"这东西到底替我省了多少事"。

        和 today_stats 分开两个接口，是为了不把原来的返回形状改掉
        （界面和测试都在读那几个键）。两个口径都由后端算，界面不自己加减。
        """
        return self.store.total_stats()

    def stats(self) -> dict:
        return self.store.stats()

    def dropped(self) -> dict:
        """废件箱：**两样东西合在一起**。

        ① 你点过「这不是待办」的待办（tasks 里 status='dropped'）
        ② 被规则拦下的原文（dropped_items 表）

        为什么要合（真踩过）：以前界面只显示 ②，于是用户点完「这不是待办」，
        那条就**哪儿都找不到了** —— 数据明明在库里，是界面没给口。
        合在一起之后还能点「恢复」把它放回主列表。
        """
        items: list[dict] = []
        for task in self.store.list_tasks("junk", limit=JUNK_LIST_MAX):
            items.append({
                "kind": "task", "id": task.task_id, "text": task.title,
                "reason": task.drop_reason or "你标成了「不是待办」",
                "source": task.source_name or "", "at": task.updated_at or "",
            })
        for row in self.store.list_dropped(limit=JUNK_LIST_MAX):
            items.append({
                "kind": "text", "id": int(row.get("drop_id") or 0),
                "text": str(row.get("text") or ""),
                "reason": str(row.get("reason") or ""),
                "source": str(row.get("source_name") or ""),
                "at": str(row.get("dropped_at") or ""),
            })
        # 谁新谁排前面。两边的字段名不一样，但都是 "YYYY-MM-DD HH:MM:SS"，能直接比。
        items.sort(key=lambda item: item["at"], reverse=True)
        return {"items": items[:JUNK_LIST_MAX]}

    def dropped_detail(self, drop_id: int) -> dict:
        """废件箱里一条"被规则拦下的原文"：**当时到底收到了什么**。

        为什么要有这个（用户原话）："我看了详情，里面还有张图片，但一看就不是
        [待办]……它是什么原因读取的？" —— 列表上只有一行 "[图片]" 是答不上来的：
        那张图当时下载了没有、里面读没读出字、发的人是谁，全在这儿摊开。
        """
        row = self.store.get_dropped(int(drop_id))
        if row is None:
            return {"ok": False, "message": "这条已经不在废件箱里了"}
        msg_id = str(row.get("msg_id") or "")
        original = self.store.get_message(msg_id) or {}
        files = self._dropped_files(msg_id)
        preview = ""
        for path in files:                  # 第一张能预览的图
            preview = _preview_of(path)
            if preview:
                break
        return {
            "ok": True,
            # 原文优先取全文（废件箱那笔截到 500 字）
            "text": str(original.get("text") or row.get("text") or ""),
            "reason": str(row.get("reason") or ""),
            "source": str(row.get("source_name") or ""),
            "sender": str(original.get("sender") or ""),
            "at": str(original.get("sent_at") or row.get("dropped_at") or ""),
            # 理解层当时算出来的结论，比如"图片里没有识别到文字"
            "notes": [str(item) for item in ((original.get("raw") or {})
                                             .get("extract_notes") or [])],
            "files": [{"name": path.name, "size": path.stat().st_size}
                      for path in files],
            "preview": preview,
        }

    @staticmethod
    def _dropped_files(msg_id: str) -> list[Path]:
        """这条消息当时取回来的附件在哪。

        QQ 来源存附件时文件名就是 `qq_<消息编号>_...`（见 sources/qq.py 的
        `save_attachment`），所以按编号就能把当时那张图找回来 ——
        不用为了废件箱再加一张表。
        """
        from ..runtime.config import attachments_dir

        match = re.fullmatch(r"qq:(\d+)", str(msg_id or ""))
        if not match:
            return []
        try:
            return sorted(attachments_dir().glob(f"qq_{match.group(1)}_*"))
        except OSError:
            return []

    # ------------------------------------------------------------------ 写
    def _remember(self, kind: str, ids) -> None:
        """记下"刚才改了什么"，给 6 秒内的「撤销」用。只留最后一次。"""
        self._last_action = {"kind": kind,
                             "ids": [int(i) for i in (ids or [])]}

    @tracked
    def undo(self) -> dict:
        """撤销上一次完成/归档/误判（界面只在那几秒内给按钮）。

        为什么要有：批量归档点错了，现在只能去废件箱/归档里一条条找回来 ——
        一次回到原样才配得上"批量"。
        """
        action = getattr(self, "_last_action", None)
        if not action:
            return {"ok": False, "message": "没有可撤销的操作"}
        ids = list(action.get("ids") or [])
        for task_id in ids:
            if action["kind"] == "archive":
                self.store.unarchive(task_id)
            else:
                self.store.set_status(task_id, "open", "撤销上一次操作")
        self._last_action = None
        self._revision += 1
        return {"ok": True, "count": len(ids)}

    def complete(self, task_id: int) -> dict:
        self.store.set_status(int(task_id), STATUS_DONE, "界面里标记完成")
        self._remember("done", [task_id])
        return {"ok": True}

    def drop(self, task_id: int) -> dict:
        """标记误判。留一笔记录，以后调规则用得上。"""
        self.store.set_status(int(task_id), STATUS_DROPPED, "界面里判定为误判")
        self._remember("drop", [task_id])
        return {"ok": True}

    def archive(self, task_id: int) -> dict:
        self.store.archive(int(task_id), "界面里归档")
        self._remember("archive", [task_id])
        return {"ok": True}

    def restore(self, task_id: int) -> dict:
        """把完成的 / 归档的 / 标成误判的，**移出到「全部未完成」**。

        **这就是"移到哪"的答案：目标只有一个 —— 全部未完成。**
        移出之后不用选去哪，它自己会按截止时间回到该去的分组
        （今天 / 本周 / 逾期…）；没写时间的就只在「全部未完成」里躺着。

        要让一条待办"变成明天的"，那是改时间（「改内容」）的事，
        不是移出的事 —— 两件事混在一起，用户会以为自己在搬文件夹。

        ⚠️ 归档的必须**额外清掉 `is_archived`**：`set_status` 不碰这个字段，
        所以以前归档的点恢复等于什么都没发生（界面上还找不着）。
        """
        task_id = int(task_id)
        self.store.set_status(task_id, STATUS_OPEN, "用户把它移出到未完成")
        self.store.unarchive(task_id)
        self._revision += 1
        return {"ok": True}

    @tracked
    def promote_drop(self, drop_id: int) -> dict:
        """废件箱里那条**其实有用** —— 把它捡回来变成待办。

        以前这儿只有一句"复制下来贴到「＋ 新建待办」里"：两步手活，
        而带附件的那种更绝 —— 正文只有 "[图片]"，**什么都复制不出来**。
        用户真报过：两条图躺在废件箱里，一条都捡不回来。
        """
        row = self.store.get_dropped(int(drop_id))
        if row is None:
            return {"ok": False, "message": "这条已经不在废件箱里了"}
        paths = [str(path) for path in self._dropped_files(str(row.get("msg_id") or ""))]
        added = int(self._act("promote_drop", row, paths) or 0)
        if added:
            # 捡回来了就别在废件箱里留一份（原始消息和附件都还在，见 forget_drop）
            self.store.delete_dropped(int(drop_id))
            self._revision += 1
        return {"ok": True, "added": added}

    def forget_drop(self, drop_id: int) -> dict:
        """「删掉它」：把这条从废件箱里清走。

        只清这一笔 —— 原始消息（messages 表）和附件都留在原地，
        所以用户清废件箱不会丢东西。
        """
        removed = self.store.delete_dropped(int(drop_id))
        self._revision += 1
        return {"ok": True, "removed": removed}

    def edit_task(self, task_id: int, title: str, due: str, location: str) -> dict:
        """手动改一条待办。

        为什么需要：解析错了一个时间，用户唯一的办法是"判误判再重新发一遍" ——
        这太蠢了。时间用浏览器的日期时间选择器（不用记格式），留空 = 没有截止时间。
        """
        from ..runtime.config import config

        clean = str(title or "").strip()
        if not clean:
            return {"ok": False, "message": "标题不能空"}
        limit = int(config.get("title", "max_len", 200) or 200)
        self.store.update_fields(int(task_id), {
            "title": clean[:limit],
            "due_at": _normalize_due(due) or None,
            "location": str(location or "").strip()[:100] or None,
        })
        self._revision += 1
        return {"ok": True}

    def set_remind(self, task_id: int, minutes) -> dict:
        """改这一条的提醒力度：截止前哪几个时间点各提醒一次。

        用户第一版的原话："旧软件那种，开始前可以选自己设定的力度，也可以是默认，
        比如 60 分钟前、30 分钟前、10 分钟前、3 分钟前"；
        第二版补了一句："要能实现多次提醒，不是只能选一个" —— 所以这里收的是
        **一组**分钟数（也收单个数字，界面早期版本传的就是单个）。
        一个都不选 = 默认：只在每日汇总里出现。
        """
        if isinstance(minutes, (list, tuple, set)):
            raw = list(minutes)
        else:
            raw = [minutes]
        leads: set[int] = set()
        for item in raw:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value < 0:
                return {"ok": False, "message": "提醒提前量不能是负数"}
            if value > 0:
                leads.add(value)
        stored = ",".join(str(m) for m in sorted(leads, reverse=True))
        # 只留一个值时也写进新列（老列留着不动，免得两处语义打架）
        self.store.update_fields(int(task_id), {"remind_leads": stored,
                                               "remind_level": 0})
        self._revision += 1
        return {"ok": True, "remind": sorted(leads, reverse=True),
                "text": describe_leads(sorted(leads, reverse=True))}

    def push_history(self) -> dict:
        """最近推过什么。用户问"手机上怎么没收到"时，第一眼看这里。"""
        return {"items": [{
            "batch": str(row.get("batch") or ""),
            "channel": str(row.get("channel") or ""),
            "at": str(row.get("pushed_at") or ""),
            "ok": bool(row.get("ok")),
            "message": str(row.get("message") or "")[:120],
        } for row in self.store.recent_pushes(limit=20)]}

    # ------------------------------------------------------------------ 附件
    def attachment_preview(self, task_id: int, path: str) -> dict:
        """给附件出个缩略图（图片才有）。界面里光写"📎1"是没法用的。"""
        target = self._known_attachment(task_id, path)
        image = _preview_of(target)
        if image:
            return {"ok": True, "image": image, "note": ""}
        note = ("这种文件没法预览，点「打开」"
                if target.suffix.lower() not in _IMAGE_MIME
                else "图太大，点「打开」看原图")
        return {"ok": True, "image": "", "note": note}

    def open_attachment(self, task_id: int, path: str) -> dict:
        """用系统默认程序打开附件。这是用户自己的文件，双击本来也能开。"""
        target = self._known_attachment(task_id, path)
        os.startfile(str(target))          # 项目只支持 Windows
        return {"ok": True}

    def _known_attachment(self, task_id: int, path: str) -> Path:
        """只允许碰**库里记着的**那个附件。

        界面上传回来的路径如果直接交给系统去打开，不校验就是一个
        "让程序打开任意文件"的后门。只认这条待办自己名下的附件。
        """
        wanted = str(path or "")
        for item in self.store.list_attachments(int(task_id)):
            if item.path != wanted:
                continue
            target = Path(item.path)
            if not target.is_file():
                raise ValueError("文件不在了（可能被移走或删掉了）")
            return target
        raise ValueError("这个附件不在库里")

    # ------------------------------------------------------------------ QQ 那一页
    @tracked
    def qq_panel(self) -> dict:
        """QQ 那一页要的全部状态：装没装、后台活没活、二维码还在不在。

        **二维码是个 PNG 文件**（NapCat 自己写到 cache/qrcode.png 的），
        直接读成 data URL 给界面显示 —— 用户不用去文件夹里找图。
        """
        import base64

        from ..runtime import deploy, desktop, heartbeat
        from ..runtime.config import config

        receiver = heartbeat.state(self.store)
        shell = deploy.find_shell_dir(deploy.default_dir())
        qr = deploy.qr_path(shell) if shell else None
        qr_info = {"ready": False, "age_seconds": None, "image": "", "path": str(qr or "")}
        if qr is not None and qr.is_file():
            age = time.time() - qr.stat().st_mtime
            qr_info["ready"] = True
            qr_info["age_seconds"] = round(age)
            # 超过 5 分钟就不给图了：NapCat 的码大约 2 分钟过期，
            # 拿一个过期的码让人扫，比不给更气人。
            if age < 300 and qr.stat().st_size < 2 * 1024 * 1024:
                blob = base64.b64encode(qr.read_bytes()).decode("ascii")
                qr_info["image"] = f"data:image/png;base64,{blob}"

        return {
            "installed": shell is not None,
            "account": int(config.get("qq", "account", 0) or 0),
            "qq_enabled": bool(config.get("qq", "enabled")),
            "receiver": receiver,
            # ⚠️ 这个和 receiver["alive"] **不是一回事**：那个是"60 秒内有没有心跳"，
            # 进程退了它还能 True 一分钟；这个直接问那把独占锁，是精确的。
            # 界面判断"后台到底还在不在"必须用这个（重启流程踩过坑）。
            "running": heartbeat.proc_alive(),
            # NapCat 自己的日志里最后一条报错，翻成人话 ——
            # 登录失败时它只写在自己日志里，不给用户看的话就是
            # "扫了码、点了确认、然后什么都没有"（2026-10-13 报的）。
            "napcat_error": deploy.napcat_error_hint(),
            "qr": qr_info,
            "autostart": desktop.autostart_enabled(),
            "blockers": self._qq_blockers(),
            # 装 NapCat / 下 QQ 时的实时进度（后台进程写文件，这里读）。
            # 没有它的话，第一次点「启动 QQ」会有好几分钟界面上什么都不动 ——
            # 用户以为卡死了，就去点第二次、第三次。
            "progress": deploy.read_progress(),
            # **登上了却收不到消息**，十有八九是这里空的。摆出来，别让用户猜：
            # 他刚扫码成功、特别期待手机上的东西，结果一条都不来。
            "scope": self._scope_summary(),
            # 后台没跑起来时把它的输出尾巴显示出来 ——
            # 不然"点了没反应"就只能靠猜。
            "log_tail": [] if receiver["alive"] else _tail(_background_log()),
        }

    @staticmethod
    def _scope_summary() -> dict:
        """现在到底"在收什么"：几个群、几个私聊、几个收件箱。

        为什么必须摆到界面上：用户扫码登录成功、左下角也显示登录号了，
        然后手机上一条都不来 —— 因为**一个群都没勾**。他只会觉得"登录了却没用"，
        而这条信息以前只在 config.toml 里躺着。
        """
        from ..runtime.config import config

        def ids(key: str) -> list:
            value = config.get("qq", key, []) or []
            return [item for item in value if str(item).strip()]

        groups = ids("group_ids")
        privates = ids("private_ids")
        inbox = ids("inbox_ids")
        all_groups = bool(config.get("qq", "receive_all", False))
        all_privates = bool(config.get("qq", "receive_private_all", False))
        parts = [
            "全部群" if all_groups else f"{len(groups)} 个群",
            "全部私聊" if all_privates else f"{len(privates)} 个私聊",
            f"{len(inbox)} 个收件箱",
        ]
        return {"text": "｜".join(parts),
                "empty": not (groups or privates or inbox
                              or all_groups or all_privates)}

    @staticmethod
    def _qq_blockers() -> list[str]:
        """启动 QQ 之前**必须**满足的条件。缺什么就返回一句人话。

        为什么必须提前查（用户报的，确实是这样）：
        `run_serve` 在没有登录号时会**跳过整套 NapCat 部署**，
        直接进"等 NapCat 连上来"的循环 —— 而那个 NapCat 永远不会启动。
        用户看到的就是"卡住"，而且没有任何提示。
        """
        from ..runtime.config import config

        problems: list[str] = []
        if not config.get("qq", "enabled"):
            problems.append("「QQ 接入」还没打开（设置 → 收消息 → QQ 接入 → 第一项改成 true）")
        if not int(config.get("qq", "account", 0) or 0):
            problems.append("没填要登录的 QQ 号（设置 → 收消息 → QQ 接入 → 「要登录哪个 QQ 号」）")
        return problems

    @tracked
    def start_qq(self) -> dict:
        """后台起一个 `run.py --serve`（收消息 + 定时推送）。

        为什么开**子进程**而不是在当前进程里起线程：界面关掉不该把收消息
        一起关掉；而且把 WebSocket 服务端和 NapCat 子进程塞进界面进程，
        界面一崩就全崩了。这也是原来的设计（两个进程），只是现在由界面代劳。
        """
        import subprocess
        import sys

        from ..runtime import deploy, heartbeat

        problems = self._qq_blockers()
        if problems:
            return {"ok": False, "blockers": problems,
                    "message": "先补上这两步再启动：" + "；".join(problems)}
        # 正在装 NapCat 时不许再起一个 —— 两个进程会在同一份目录上一起装。
        # （装的过程会持续报心跳，一般轮不到这里；这是最后一道闸。）
        if deploy.read_progress().get("active"):
            return {"ok": False, "message": "正在装 NapCat，等它装完再点（QQ 那一页有进度）"}
        # ⚠️ 用 proc_alive 而不是心跳的 alive：后者有 60 秒宽限，
        # 刚点过「重新出码」的话它还是 True，于是这里会拒绝启动 ——
        # 而旧进程其实已经退了，结果就是"后台没在跑"（真踩过）。
        if heartbeat.proc_alive():
            return {"ok": False, "message": "后台已经在跑了"}

        # 旧二维码先删掉（见 deploy.clear_qr 的注释）。
        shell = deploy.find_shell_dir(deploy.default_dir())
        if shell is not None:
            deploy.clear_qr(shell)

        from ..runtime.desktop import frozen_exe

        project = _plugin_root()
        program = frozen_exe()
        if program is None:
            # 源码运行：pythonw.exe + run.py --serve（打包后不用这一套）
            runner = project / "run.py"
            if not runner.is_file():
                return {"ok": False, "message": f"找不到 {runner}"}
            interpreter = Path(sys.executable).with_name("pythonw.exe")
            program = interpreter if interpreter.is_file() else Path(sys.executable)
            argv = [str(program), str(runner), "--serve"]
        else:
            argv = [str(program), "--serve"]      # 打包后：exe 自己带参数跑
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0))
        log = _background_log()
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            sink = open(log, "w", encoding="utf-8", errors="replace")
        except OSError:
            sink = None
        try:
            subprocess.Popen(
                argv, cwd=str(project),
                creationflags=flags, stdin=subprocess.DEVNULL,
                # 输出写进日志文件，别丢 —— 界面在它没起来时会把尾巴显示出来。
                stdout=sink or subprocess.DEVNULL, stderr=subprocess.STDOUT,
                # 子进程的输出编码得钉死 UTF-8：重定向到文件时它默认按
                # 系统代码页（这台是 GBK）写，读的时候按 UTF-8 读就全乱了。
                env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                close_fds=True,
            )
        except OSError as exc:
            return {"ok": False, "message": f"起不来：{exc}"}
        finally:
            if sink is not None:
                sink.close()
        return {"ok": True, "lazy": True,
                "message": "后台起来了。第一次要等几十秒下载安装 NapCat，"
                           "完了二维码会出现在这里。"}

    @tracked
    def stop_background(self) -> dict:
        """让后台进程自己收工（写个文件，它自己看到就退）。

        为什么不直接杀进程：得先确认那个 PID 确实是我们的后台进程，
        查错了就是误杀别人的程序。写文件让它自己看，简单也安全。
        """
        from ..runtime import heartbeat
        from ..runtime import deploy

        if not heartbeat.proc_alive():
            # 本来就没在跑：顺手把上次留下的停止请求清掉。不清的话，
            # 下次启动后台会被那个旧文件一上来就劝退（这个坑记过两次了）。
            heartbeat.clear_stop()
            # 后台没了、QQ 可能还活着（启动器退出后它就成了孤儿）——
            # 既然用户按了「停止」，就连它们一起收掉，别在后台占内存。
            shell = deploy.find_shell_dir(deploy.default_dir())
            killed = deploy.stop_all(shell) if shell is not None else 0
            tail = f"，顺手清掉了 {killed} 个残留 QQ 进程" if killed else ""
            return {"ok": True, "stopped": True,
                    "message": "后台本来就没在跑" + tail}
        heartbeat.request_stop()
        return {"ok": True, "message": "已经让它收工，几秒钟后停"}

    def set_autostart(self, enabled: bool) -> dict:
        """开/关开机自启（往「启动」文件夹里放/删一个快捷方式）。"""
        from ..runtime import desktop

        ok, message = desktop.set_autostart(bool(enabled))
        return {"ok": ok, "message": message}

    def locations(self) -> dict:
        """东西都装在哪儿、怎么卸干净。设置里那一块就靠它。"""
        from ..runtime import deploy, desktop
        from ..runtime.config import config, default_data_dir

        return {
            "root": str(_plugin_root()),
            "data": str(default_data_dir()),
            "napcat": str(deploy.default_dir()),
            "config": str(config.path or ""),
            "autostart": desktop.autostart_enabled(),
        }

    # ------------------------------------------------------------------ 环境
    @tracked
    def config_summary(self) -> dict:
        """界面上显示"现在什么状态"用。"""
        from ..runtime import update
        from ..runtime.config import config, default_data_dir
        from ..runtime import heartbeat
        from ..runtime.heartbeat import state as beat_state
        from ..runtime.sysmon import describe as mem_describe
        from ..runtime.sysmon import memory_state, self_rss_mb

        receiver = beat_state(self.store)
        system = memory_state() or {}
        return {
            "memory": mem_describe(),
            # 用户要看"这东西到底吃多少"：界面进程自己 + 后台进程（后台从心跳报上来）。
            # 后台读不到就是它没在跑，界面上那一栏不显示它就行。
            "memory_percent": system.get("percent"),
            "ui_mb": self_rss_mb(),
            "background_mb": receiver.get("rss_mb"),
            "qq_groups": len(config.get("qq", "group_ids", []) or []),
            "push_times": config.get("push", "times", []),
            # 推送真的能推出去吗（企业微信或通用地址，填了哪个都算）。
            # 不为真时左下角要提醒一句 —— 不然用户填完地址以为配好了，
            # 手机上永远什么都没有。
            "push_ready": push_ready(),
            # 没配好时那一行点下去要跳到设置的哪一段（见 sinks/router.py）
            "push_section": push_help_section(),
            # 界面拖文件时先在前端拦一道，免得白传一遍几十 MB
            "drop_max_mb": int(config.get("ui", "drop_max_mb", 12) or 12),
            # 有没有新版本（只读缓存，界面永远不等网络）
            "update": update.summary(),
            # ⚠️ 这是**状态**，不是配置：收消息的进程还在不在。
            # 上面那个 qq_groups 只是"你配了几个群"，跟进程活没活着没关系。
            "receiver": receiver,
            # 进程**真的**还在吗（问那把独占锁，不是"心跳新不新鲜"）。
            # 两个都要：进程没了要立刻说"没在跑"，进程还在但不报心跳
            # 说明它卡住了 —— 那是另一句话。
            "running": heartbeat.proc_alive(),
            "qq_enabled": bool(config.get("qq", "enabled")),
            # AI 增强到底接上没接上。**光看开关是不够的**：地址或模型名有一个
            # 空着，插件就安静地跳过（2026-10-14 用户被这么坑了一整天 ——
            # "我开了AI增强，你看看有没有用"，而左下角当时一个字都不显示）。
            "ai_enabled": bool(config.get("ai", "enabled")),
            "ai_model": str(config.get("ai", "model", "") or "").strip(),
            "ai_ready": _ai_ready(config),
            "ai_why": _ai_why(config),
            "db": str(getattr(self.store, "conn", "")),
        }

    def plugins(self) -> dict:
        """装了哪些插件、哪个坏了。坏的要在界面上看得见（契约第二节）。

        连插件文件夹一起给出去：光有数量没用，用户还想知道**往哪儿装**。
        """
        from ..runtime.plugins import plugin_dir

        return {"items": [
            {"name": p.name, "title": p.title, "ok": p.ok, "error": p.error,
             "section": getattr(p, "section", ""),
             "description": getattr(p, "description", "")}
            for p in self._plugins
        ], "folder": str(plugin_dir(_plugin_root()))}

    # ------------------------------------------------------------------ 设置
    def settings(self) -> dict:
        """设置面板要的全部内容：分组 → 段 → 项，带当前值、说明、是否高级、是否锁住。

        清单直接从 config 的默认值生成，所以界面**不会漏项** ——
        以后加了新配置，界面上自动就出现。
        """
        from ..runtime.config import config
        from ..runtime import heartbeat
        from ..version import version_text

        return {"groups": config.schema(), "path": str(config.path or ""),
                # 版本号摆在设置页顶上：用户报问题时第一句就是"我是哪一版"，
                # 不然我得靠猜（以前真猜过）。
                "version": version_text(),
                # 后台跑着的配置跟文件里的**已经不一样了**吗（只比要重启那几项）。
                # 以前界面是在保存那一刻记一个内存标记，于是：
                #   · 用别的方式重启（重新出码）之后它不会消；
                #   · 顶部那句"带「要重启」的那几项按提示重启"是**死文案**，
                #     永远挂在那儿 —— 用户看到的是"每次打开他还在"。
                # 现在这句提示由这里说了算，重启完自然就没了。
                "restart_needed": heartbeat.restart_needed(self.store)}

    @tracked
    def save_settings(self, updates: dict) -> dict:
        """把界面上改动过的项写回 config.toml。写回是按行改，不动用户的注释。"""
        from ..runtime.config import config

        changed: list[str] = []
        for dotted, value in (updates or {}).items():
            section, _, key = str(dotted).partition(".")
            if not section or not key:
                continue
            config.set(section, key, value)
            changed.append(dotted)
        self._revision += 1
        return {"ok": True, "changed": changed, "path": str(config.path or "")}

    @tracked
    def test_ai(self) -> dict:
        """试一下 AI 通不通：发一句固定的话，看模型回不回、回得多快。

        为什么需要：填完地址/模型/密钥的人没法知道对不对，只能干等一条长消息
        （最短也要等到群里发一条 40 字以上的通知）。模型名抄错、"免费模型改名了"、
        地址少了 /v1 —— 都是点这一下就看得出来的事。

        发出去的是**一句固定的话**，不是用户内容，所以不用先打开「允许上传」；
        通了之后还会提醒他"开关没全开的话，现在其实还没在用"。
        """
        from ..runtime.ai_probe import probe

        return probe()

    @tracked
    def test_push(self) -> dict:
        """发一条测试推送，看看地址到底通不通。

        为什么要有这个：刚填完地址的人没法知道对不对，只能等下一次定时推送
        （最长 12 小时）。钉钉的「关键词没对上」（errcode 310000）、
        通用地址的 token 抄错一位，都是点这一下就看得出来的事。

        ⚠️ 读的是**已经保存的**配置。刚改完地址还没点保存的话，
        这里当然还是旧的 —— 所以下面那句话里特地点了「保存」。
        """
        from ..contracts.models import PushPayload
        from ..sinks import build_sink

        sink = build_sink()
        channel = getattr(sink, "name", "console")
        if channel == "console":
            return {"ok": False, "channel": channel,
                    "message": "还没配推送地址（或者刚改完没点「保存」）——"
                               "先填地址、点保存，再点这个按钮。"}

        payload = PushPayload(channel=channel, kind="text",
                              title="私人秘书 · 测试推送",
                              body="看到这条就说明通道通了。\n"
                                   "以后每天的汇总会按你设的时间发过来。")
        try:
            result = sink.deliver(payload)
        except Exception as exc:
            return {"ok": False, "channel": channel,
                    "message": f"{type(exc).__name__}: {exc}"}

        explain = getattr(sink, "explain", None)
        message = (explain(result) if (explain and not result.ok) else result.message)
        return {"ok": bool(result.ok), "channel": channel,
                "message": message or ("发出去了" if result.ok else "发送失败")}

    @tracked
    def qq_groups(self) -> dict:
        """后台读到的"登录的号加过哪些群"，给设置页做勾选。

        为什么要绕这么一圈：群列表只能从 QQ 那边问出来（get_group_list），
        而跟 QQ 说话的是**后台那个进程** —— 界面这边没有连接。所以后台把列表
        写进心跳，界面读心跳（见 runtime/heartbeat.py 和 qq.QQSource.groups）。

        返回里带 `alive`：没在跑的时候界面得说清楚"为什么读不到"，
        而不是丢一个空列表给用户猜。
        """
        from ..runtime import heartbeat
        from ..runtime.config import config

        beat = heartbeat.state(self.store)
        groups = [g for g in (beat.get("groups") or []) if g.get("id")]
        try:
            selected = [int(x) for x in (config.get("qq", "group_ids") or [])]
        except (TypeError, ValueError):
            selected = []
        return {"groups": groups, "selected": selected,
                # 用精确的那个判断：进程真没了就说"没在跑"，
                # 而不是拿 60 秒宽限里的"还活着"糊弄过去
                "alive": heartbeat.proc_alive(), "at": str(beat.get("at") or "")}

    # ------------------------------------------------------------------ 更新
    @staticmethod
    def _update_repo() -> str:
        from ..runtime.config import config

        return str(config.get("ui", "update_source", "") or "").strip()

    @tracked
    def about(self) -> dict:
        """「关于」面板要的东西：版本、构建签名、官方地址、数据在哪。

        为什么要摆出来：开源之后一定有人打包去卖。这段话是**给买家看的** ——
        他打开关于页就知道正版免费、签名对不对、数据在哪个文件夹。
        """
        import sys

        from ..runtime.config import default_data_dir, default_root
        from ..version import VERSION, version_text

        root = default_root()
        info = root / "build-info.txt"
        built = ""
        if info.is_file():
            try:
                built = info.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                built = ""
        signed = "签名" in built and "未签名" not in built
        return {
            "version": VERSION,
            "version_text": version_text(root),
            "build": built,
            "signed": signed,
            "repo": "https://github.com/Landercey/personal-secretary",
            "data": str(default_data_dir()),
            "root": str(root),
            "frozen": bool(getattr(sys, "frozen", False)),
        }

    @tracked
    def check_update(self) -> dict:
        """手动问一次有没有新版本（左下角那次是后台自动的，这个是按钮点的）。

        顺带把结果写进缓存 —— 左下角那行读的就是缓存，于是手动查完它也跟着变。
        """
        from ..runtime import update
        from ..version import VERSION

        repo = self._update_repo()
        if not repo:
            return {"ok": False, "message": "没配更新源（设置 → 界面与更新）"}
        from ..runtime import selfupdate

        # 只问版本号（走 raw 文件那条路，不受接口限流影响 —— 见 selfupdate.patch_url）
        latest = update.fetch_latest(repo)
        if not latest:
            return {"ok": False,
                    "message": "问不到 GitHub。可能是断网，也可能是 GitHub 临时限流"
                               "（未登录的接口每小时只给 60 次）。"
                               "如果你用代理/加速器，确认它开着（要开「系统代理」）。"
                               "过一会儿再试，不影响你现在用。"}
        update.write_cache(latest)
        has_new = update.is_newer(latest, VERSION)
        return {"ok": True, "current": VERSION, "latest": latest, "has_new": has_new,
                "page": f"https://github.com/{repo}/releases",
                "patch": selfupdate.patch_url(repo, latest),
                "message": (f"有新版本 v{latest}（你现在 v{VERSION}）" if has_new
                            else f"已经是最新的（v{VERSION}）")}

    @tracked
    def install_update(self) -> dict:
        """一键更新：下「一键升级包」→ 让它去关旧版、覆盖、重开。

        **这个调用返回之后界面就该退出了**（升级包会先 taskkill 掉我们）。
        覆盖的只有 `私人秘书.exe` 和 `_internal/`，配置、待办、插件、
        下好的 QQ 都在别的文件夹里 —— 所以**不用重新扫码登录**。

        为什么不自己写批处理等 PID 再 tar（2026-10-17 实测）：detached 起 cmd 时
        `tasklist | find` 那个管道会卡死 —— 程序被自己关了、文件没换成、也没重开。
        升级包这条路由"打包—覆盖—重启"一次跑通，直接把活交给它。
        """
        from ..runtime import selfupdate, update
        from ..runtime.config import default_data_dir, default_root

        repo = self._update_repo()
        if not repo:
            return {"ok": False, "message": "没配更新源（设置 → 界面与更新）"}
        latest = update.fetch_latest(repo)
        if not latest:
            return {"ok": False,
                    "message": "问不到 GitHub（断网或临时限流）。"
                               "如果你用代理/加速器，确认它开着；过会儿再试。"}
        # 地址按版本号拼，不问接口 —— 见 selfupdate.upgrade_url 的注释
        url = selfupdate.upgrade_url(repo, latest)
        exe = default_data_dir() / "updates" / f"一键升级-v{latest}.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        # 上次更新留下的那份（45 MB）顺手清掉，别一次一次攒在 data 里
        for old in exe.parent.glob("一键升级-*.exe"):
            old.unlink(missing_ok=True)
        try:
            selfupdate.download(url, exe)
        except Exception as exc:
            return {"ok": False, "message": f"下载升级包失败：{type(exc).__name__}: {exc}"}
        try:
            # 升级包自己会：关掉正在跑的 私人秘书 → 覆盖程序文件 → 重新打开。
            selfupdate.run_upgrader(exe, default_root())
        except OSError as exc:
            return {"ok": False, "message": f"起不来升级包：{exc}"}
        return {"ok": True, "restart_receiver": True,
                "message": "开始更新：会弹一个升级窗口，它负责关掉旧版、覆盖、再打开。"
                           "配置和待办都不动，也不用重新扫码登录。"}

    def quit_app(self) -> dict:
        """把界面关掉。一键更新用得上：脚本在等这个进程消失。"""
        # 名字带下划线是硬要求，别改回 `window`：pywebview 会遍历 js_api 的
        # 公开属性去生成 JS 代理，碰到窗口就会钻 .NET 对象把界面卡死（见 run.py
        # open_ui 里 api._window 那段说明）。
        window = getattr(self, "_window", None)
        if window is None:
            return {"ok": False, "message": "拿不到窗口（源码直接跑的时候没有）"}
        self._dispose_tray()
        window.destroy()
        return {"ok": True}

    # ------------------------------------------------------------------ 托盘图标
    def tray(self, tooltip: str = "") -> dict:
        """在任务栏托盘放个小图标，鼠标放上去就知道后台在不在跑。

        为什么要有（2026-10-14 用户："不知道有没有在跑"）：窗口一关，这个程序
        就彻底看不见了，用户只能靠猜。托盘上有个图标，hover 一下就有答案。

        为什么能写在这儿：pywebview 的 js_api 调用跑在 WinForms 线程上，
        而 NotifyIcon 必须建在有消息循环的线程上。界面每 5 秒调一次，
        第一次把它建出来，之后只改提示文字；失败也只记一句，不影响界面。
        """
        try:
            if self._tray is None:
                self._tray = self._make_tray()
            if self._tray is not None and tooltip:
                self._tray.Text = str(tooltip)[:63]       # .NET 上限 63 个字符
            return {"ok": self._tray is not None, "message": self._tray_note}
        except Exception as exc:                          # noqa: BLE001
            self._tray_note = f"{type(exc).__name__}: {exc}"
            return {"ok": False, "message": self._tray_note}

    def _make_tray(self):
        """把 NotifyIcon 建出来。菜单两项：打开界面 / 退出界面。"""
        import clr

        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        from System.Drawing import Icon
        from System.Windows.Forms import (ContextMenuStrip, NotifyIcon,
                                          ToolStripMenuItem)

        def on_open(*_args) -> None:
            try:
                self._show_window()
            except Exception:                             # noqa: BLE001
                pass

        def on_quit(*_args) -> None:
            try:
                self.quit_app()
            except Exception:                             # noqa: BLE001
                pass

        menu = ContextMenuStrip()
        open_item = ToolStripMenuItem("打开界面")
        open_item.Click += on_open
        quit_item = ToolStripMenuItem("退出界面（后台照常收消息）")
        quit_item.Click += on_quit
        menu.Items.Add(open_item)
        menu.Items.Add(quit_item)

        tray = NotifyIcon()
        tray.Icon = self._tray_icon(Icon)
        tray.ContextMenuStrip = menu
        tray.DoubleClick += on_open
        tray.Text = "私人秘书"
        tray.Visible = True
        return tray

    @staticmethod
    def _tray_icon(icon_cls):
        """托盘图标：打包版用 exe 自带的那个，源码版用 assets/app.ico。"""
        import sys

        if getattr(sys, "frozen", False):
            return icon_cls.ExtractAssociatedIcon(sys.executable)
        ico = Path(__file__).resolve().parents[2] / "assets" / "app.ico"
        if ico.is_file():
            return icon_cls(str(ico))
        return icon_cls.ExtractAssociatedIcon(sys.executable)

    def _show_window(self) -> None:
        window = getattr(self, "_window", None)
        if window is None:
            return
        try:
            window.show()
        except Exception:                                 # noqa: BLE001
            window.restore()

    def _dispose_tray(self) -> None:
        """退出时收掉托盘图标，别在托盘里留个"幽灵"（要 hover 一下才消失）。"""
        tray, self._tray = self._tray, None
        if tray is None:
            return
        try:
            tray.Visible = False
            tray.Dispose()
        except Exception:                                 # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 新手引导
    def setup_state(self) -> dict:
        """还差哪几件事没做。界面拿它决定"新手引导"留不留。"""
        from ..runtime.config import config
        from ..runtime.heartbeat import state as heartbeat_state

        def has(section: str, key: str) -> bool:
            return bool(str(config.get(section, key, "") or "").strip())

        def qq_in_use() -> bool:
            """这套配置到底用不用 QQ。用不着的话，跟 QQ 有关的几条就不列出来。"""
            return bool(config.get("qq", "enabled")
                        or config.get("qq", "group_ids")
                        or config.get("qq", "private_ids")
                        or config.get("qq", "inbox_ids")
                        or config.get("qq", "receive_all"))

        checks = [
            {"key": "risk", "section": "risk", "label": "看过风控提示（小号还是大号）",
             "ok": bool(config.get("risk", "confirmed_secondary_account"))},
            {"key": "push", "section": push_help_section(),
             # ⚠️ 光是填了地址还不够：`enabled` 默认是 **false**，
             # 不打开的话推送会退回控制台 —— 界面上什么都看不出来，
             # 用户填完地址、清单打勾、然后手机上永远收不到。
             # 这两项必须一起查（真踩过这一类的静默失败）。
             # 企业微信和通用地址都算：填了哪个走哪个。
             "label": "填了推送地址，并且把推送打开（两项都要）",
             "ok": push_ready()},
            # QQ 号必须**在启动之前**填好：没填的话 `run_serve` 会跳过整套
            # NapCat 部署直接去等连接，那个 NapCat 永远不启动，看着就是"卡住"。
            *([{"key": "qq_account", "section": "qq",
                "label": "填了要登录的 QQ 号（不填「启动 QQ」会卡住；"
                         "这个号要先用电脑上的 QQ 退出登录）",
                "ok": bool(int(config.get("qq", "account", 0) or 0))}]
              if qq_in_use() else []),
            {"key": "qq", "section": "qq", "label": "选了要收的群或私聊",
             "ok": qq_in_use()},
            # ⚠️ **这一步以前没有**（用户问"安装好的教程做了吗"时才发现的）：
            # 配置填完 ≠ 能收到消息 —— 还得把后台起起来、扫码登录。
            # 少了它，用户填完所有东西、清单一消失就不知道该干嘛了，
            # 干等半天手机上什么都没有。
            # 点它要跳到「📱 QQ 接收」那一栏（不是设置），所以带个 panel 标记。
            *([{"key": "qq_login", "panel": "qq",
                "label": "启动 QQ 并扫码登录（点这里）",
                "ok": bool(heartbeat_state(self.store).get("alive"))}]
              if qq_in_use() else []),
            # AI 不在这里列 —— 它是**可选**功能（默认关着也能正常用）。
            # 清单只放"不做就用不起来"的步骤；塞一个可选项进去，等于催用户
            # 去填一堆接口地址，纯属添乱。要开它，去 设置 → 看懂消息 → AI 增强。
        ]
        return {"checks": checks,
                "done": all(item["ok"] for item in checks if not item.get("optional")),
                # 启动自检的问题（库坏了/盘满了/安全模式）+ 配置读坏了的说明。
                # 界面把它们当提示条显示 —— 不然用户只会觉得"设置怎么全没了"。
                "notes": (list(self._notices)
                          + ([{"code": "CONFIG.LOAD.FAILED", "level": "warn",
                               "message": config.load_error}]
                             if getattr(config, "load_error", "") else [])),
                "suggestion": self._suggestion()}

    def _suggestion(self) -> dict | None:
        """一条**等时机到了才出现**的建议：现在该去调推送时间了。

        为什么不在首次启动就问：用户还没收到过任何推送，不知道这东西长什么样，
        让他选时间等于让他凭空决定 —— 他只会乱点，或者卡在这一步不走了。
        **等第一条推送真的到了手机上**，他才知道"哦，是这么一个东西"，
        这时候问才有意义。

        为什么不用记一个"问过了"的标记：**推送时间还是默认值就说明他没改过**。
        他一改，这条建议自然消失。一个状态都不用存。
        """
        from ..runtime.config import DEFAULTS, config

        if not self._pushed_once():
            return None
        current = list(config.get("push", "times", []) or [])
        if current != list(DEFAULTS["push"]["times"]):
            return None
        return {"section": "push",
                "text": f"手机应该收到过推送了。现在是每天 {' / '.join(current)}，"
                        f"想换时间点就点这里"}

    def _pushed_once(self) -> bool:
        """有没有**成功**推出去过至少一次。"""
        if self.store is None:
            return False
        try:
            return any(item.get("ok") for item in self.store.recent_pushes(limit=5))
        except Exception:
            return False

    def confirm_risk(self) -> dict:
        """用户在风控提示上点了「我知道了」。

        这是契约第七节的红线：首次启动必须确认过。
        写进配置之后，以后不再弹。
        """
        from ..runtime.config import config

        config.set("risk", "confirmed_secondary_account", True)
        self._revision += 1
        return {"ok": True}

    # ------------------------------------------------------------------ 录入选
    @tracked
    def add_text(self, text: str) -> dict:
        """手动粘贴一条。走的是和 QQ 消息**完全相同**的那条管线。"""
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "message": "先写点什么，再点添加"}
        added = self._act("add_text", text) or 0
        self._revision += 1
        return {"ok": True, "added": added}

    @tracked
    def sync_now(self) -> dict:
        """立刻拉一遍插件来源（课表这类需要主动去拿的）。"""
        result = self._act("sync") or {}
        if not isinstance(result, dict):          # 兜底：老装配点只返回条数
            result = {"added": result, "notes": []}
        self._revision += 1
        return {"ok": True, "added": int(result.get("added") or 0),
                "notes": list(result.get("notes") or [])}

    # ------------------------------------------------------------------ 拖进来的东西
    @tracked
    def stage_drop(self, name: str, data_base64: str) -> dict:
        """把拖进窗口的文件先落到暂存区，等用户选"怎么处理"。

        **为什么不在拖进来那一刻就解析**：解析要跑 OCR / PDF，大文件好几秒，
        而用户可能只是想看看。先落盘、给选项，点了"变成待办"再干活。

        **为什么内容是 base64**：网页拿不到拖进来文件的真实路径
        （浏览器安全限制，WebView2 也一样）。所以只能把文件内容摊成文本传过来。
        代价是有大小上限，见 `ui.drop_max_mb`。
        """
        from ..runtime.config import config, default_data_dir

        limit_mb = int(config.get("ui", "drop_max_mb", 12) or 12)
        # 前端已经拦过一道，这里是第二道 —— 不能让一个手写的 200 MB 把内存吃光
        if len(str(data_base64 or "")) > (limit_mb + 1) * 1024 * 1024 * 4 // 3:
            raise ValueError(f"文件超过 {limit_mb} MB，用 python run.py --file 路径")
        try:
            blob = base64.b64decode(str(data_base64 or ""), validate=True)
        except Exception:
            raise ValueError("文件内容传坏了，再拖一次试试")
        if not blob:
            raise ValueError("这个文件是空的")
        if len(blob) > limit_mb * 1024 * 1024:
            raise ValueError(f"文件超过 {limit_mb} MB，用 python run.py --file 路径")

        # 文件名要洗干净再拼进路径：用户的文件名里可能带路径分隔符或怪字符
        safe = re.sub(r"[^\w.\-]+", "_", Path(str(name or "")).name)[:64].strip("._- ")
        token = f"{int(time.time())}-{hashlib.sha1(blob).hexdigest()[:10]}-{safe or 'drop'}"
        folder = default_data_dir() / self.DROP_DIR
        folder.mkdir(parents=True, exist_ok=True)
        (folder / token).write_bytes(blob)
        return {"id": token, "name": token.split("-", 2)[2],
                "size": len(blob), "limit_mb": limit_mb}

    @tracked
    def ingest_drop(self, token: str) -> dict:
        """把暂存的文件**挪进附件目录**再入库。

        为什么先挪：暂存区是临时的，待办要是指着暂存区，哪天清理就把附件清了。
        挪的是同一个盘、同一次改名，不花时间也不占双份空间。
        """
        import shutil

        from ..runtime.config import attachments_dir

        staged = self._drop_path(token)
        path, _note = self._dropped_file(token)
        archive = attachments_dir()
        archive.mkdir(parents=True, exist_ok=True)
        # 文件名沿用暂存的那个"时间戳-指纹-原名"，重名也不会互相覆盖
        prefix = "-".join(staged.name.split("-", 2)[:2])
        target = archive / f"{prefix}-{path.name}"
        try:
            if path == staged:
                path.replace(target)          # 我们自己的暂存副本，挪走
            else:
                shutil.copy2(path, target)    # 快捷方式指向的是用户的原件，只复制
        except OSError:
            target = path                     # 挪不动就用原地，总比入库失败强
        result = self._act("ingest_file", str(target))
        if isinstance(result, dict):              # 新形状：带"为什么没读出"的原因
            added = int(result.get("added") or 0)
            notes = [str(item) for item in (result.get("notes") or [])]
        else:                                     # 兜底：老装配只返回个数
            added, notes = int(result or 0), []
        self._revision += 1
        return {"ok": True, "added": added, "notes": notes}

    def peek_drop(self, token: str) -> dict:
        """只把文件里的字读出来，不入库（界面上"只看写了什么"）。

        连**为什么没读出来**一起返回。只回一个空字符串的话，
        用户看到的永远是"这个文件里没读出文字"，
        分不清是扫描件、旧版 Office，还是不支持的格式。
        """
        path, note = self._dropped_file(token)
        result = self._act("read_file_text", str(path))
        if not isinstance(result, dict):          # 兜底：老的 action 只返回字符串
            result = {"ok": bool(result), "text": str(result or ""), "note": ""}
        text = str(result.get("text") or "")
        return {"ok": True, "chars": len(text), "text": text[:1500],
                "note": str(result.get("note") or ""),
                "method": str(result.get("method") or ""),
                "source": note}

    def _dropped_file(self, token: str) -> tuple[Path, str]:
        """暂存的那份 → 真正要读的文件。

        拖进来的经常是**桌面上的快捷方式**（.lnk，1 KB 的指路牌），
        它自己当然没字。Windows 双击会跟过去，这里也跟过去。
        """
        path = self._drop_path(token)
        if path.suffix.lower() != ".lnk":
            return path, ""

        from ..runtime.desktop import resolve_shortcut

        target = resolve_shortcut(path)
        if target is None:
            raise ValueError("这是一份快捷方式（.lnk），但读不出它指向哪儿")
        if not target.is_file():
            raise ValueError(f"这份快捷方式指向的不是文件：{target}")
        return target, f"这是一份快捷方式，真正读的是：{target}"

    def discard_drop(self, token: str) -> dict:
        """算了，不处理了 —— 把暂存的那份删掉。"""
        try:
            self._drop_path(token).unlink()
        except (ValueError, OSError):
            pass
        return {"ok": True}

    # ------------------------------------------------------------------ 装插件
    def install_drop(self, token: str) -> dict:
        """把拖进来的 `.py` 装成插件：拷进 plugins/，**当场试着加载一次**。

        为什么当场试：装完不生效、用户重启一看"没反应"，比装之前就拦下来差得多。
        加载失败就把文件删掉 —— 不留半个坏插件在他文件夹里。

        ⚠️ 插件就是一段 Python 代码，跑起来跟你自己写的程序权限一样。
        界面上必须把这句话说给用户听（见 index.html 的拖放面板）。
        """
        import shutil

        from ..runtime.plugins import Registry, plugin_dir
        from ..runtime.plugins import load as load_plugins

        staged = self._drop_path(token)
        if staged.suffix.lower() != ".py":
            raise ValueError("插件必须是 .py 文件")

        root = _plugin_root()
        folder = plugin_dir(root)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / _drop_name(token)
        if target.exists():
            # 不覆盖同名文件：那可能就是他改过的
            return {"ok": False,
                    "message": f"已经有个同名的插件了（{target.name}）—— 先删掉或者改个名"}

        shutil.copy2(staged, target)
        statuses = [s for s in load_plugins(root, Registry()) if s.name == target.stem]
        failed = [s for s in statuses if not s.ok]
        if failed or not statuses:
            target.unlink(missing_ok=True)
            why = failed[0].error if failed else "这个文件里没有 register(registry)"
            return {"ok": False, "message": f"这个插件有问题，没装上：{why}"}

        self._revision += 1
        return {"ok": True, "name": target.stem, "title": statuses[0].title,
                "message": f"装好了：{statuses[0].title}。"
                           f"重开一次程序它才会开始干活。"}

    def open_plugin_folder(self) -> dict:
        """用资源管理器打开插件文件夹 —— 想自己放文件的人需要它。"""
        from ..runtime.plugins import plugin_dir

        folder = plugin_dir(_plugin_root())
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))
        return {"ok": True, "path": str(folder)}

    @tracked
    def export_ical(self) -> dict:
        """把写了时间的待办导出成 .ics，手机日历双击就能导入。

        为什么不自动同步：那要接每个平台的账号，还要处理冲突，
        对一个本地小工具来说代价太大 —— 导出一次、导进手机，够用了。
        """
        import datetime as dt

        from ..runtime.config import default_data_dir
        from ..sinks.ical import build

        rows = [t for t in self.store.list_tasks("open", limit=2000)
                if (t.due_at or "").strip()]
        if not rows:
            return {"ok": False, "message": "还没有写了时间的待办 —— 日历里放不下「没时间」的事"}
        path = default_data_dir() / f"待办-{dt.date.today():%Y%m%d}.ics"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(build(rows), encoding="utf-8")
        return {"ok": True, "path": str(path), "count": len(rows),
                "message": f"已导出 {len(rows)} 条"}

    def open_url(self, url: str) -> dict:
        """用系统浏览器打开一个链接（目前只给「有新版本」那一行用）。

        为什么限定域名：这个接口是**网页那边调过来的**，虽然说到底是本地窗口、
        网页也是我们自己的，但"网页让程序开什么就开什么"不是个好习惯 ——
        白名单一行，省掉以后有人往里塞怪东西的麻烦。
        """
        import webbrowser

        target = str(url or "").strip()
        # 白名单：项目地址 + 爱发电（打赏页）。多一个都不放。
        if not target.startswith(("https://github.com/", "https://afdian.com/")):
            return {"ok": False, "message": "只允许打开项目地址或打赏页"}
        webbrowser.open(target)
        return {"ok": True, "url": target}

    SUPPORT_URL = "https://afdian.com/a/landercey"

    @tracked
    def support(self) -> dict:
        """要不要显示"请作者喝瓶冰糖雪梨"。

        时机：**推成功过至少一次之后**才显示（`_pushed_once`）。
        还没收到过任何推送的人，先让他把东西用起来 —— 这时候提打赏只会招人烦。
        """
        return {"ready": self._pushed_once(), "url": self.SUPPORT_URL}

    # ------------------------------------------------------------------ 意见反馈
    @tracked
    def feedback_report(self, note: str = "", include_junk: bool = False) -> dict:
        """生成一份可以复制走的反馈，**默认脱敏**。

        为什么不"帮我直接发出去"：
          · 没有服务器；要发就得先有一个，那是另一个项目的工作量。
          · 更要紧的是**隐私**：自动上报意味着用户一按按钮，
            他的报错内容（可能带群名、消息原文、webhook）就上传了。
            开源小工具里"自动上报"是减分项，不是加分项。
        所以做成：自动把该带的信息整理好、把号码打掉，用户自己看一眼、复制、贴过去。
        """
        import platform
        import sys

        from ..runtime.config import config, default_data_dir
        from ..runtime.heartbeat import state as beat_state
        from ..runtime.sysmon import memory_state, self_rss_mb
        from ..runtime import uilog
        from ..version import version_text

        root = _plugin_root()
        receiver = beat_state(self.store)
        system = memory_state() or {}
        plugins = self.plugins()["items"]
        qq = "qq"

        def count(key: str) -> int:
            value = config.get(qq, key, []) or []
            return len(value) if isinstance(value, list) else 0

        own_mb = (self_rss_mb() or 0) + (receiver.get("rss_mb") or 0)
        # 构建标识。**这条很重要**：用户报问题时我第一眼要看的就是"他装的是哪一版"。
        # 打包版读 build-info.txt（打脚本写的，含时分和提交号）；没有就退回文件时间。
        built = ""
        info = root / "build-info.txt"
        if info.is_file():
            try:
                built = info.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                built = ""
        if not built:
            try:
                target = (Path(sys.executable) if getattr(sys, "frozen", False)
                          else root / "run.py")
                built = datetime.fromtimestamp(
                    target.stat().st_mtime).strftime("%Y-%m-%d")
            except (OSError, ValueError):
                built = "?"

        lines = [
            "【私人秘书 · 反馈】",
            # 版本号放最前面：用户报问题时我第一眼要看的就是"他是哪一版"
            f"版本：{version_text(root)}",
            f"构建日期：{built}",
            # 这两行是给"两份程序 / 在压缩包里跑"这类问题准备的：
            # 路径里有 \Temp\ 就是压缩包直接双击；两个路径不一样说明装了两份。
            # 用户名要打掉 —— 反馈多半会贴到群里或 Issue 上。
            f"程序位置：{_redact(_mask_user(str(Path(sys.executable).parent if getattr(sys, 'frozen', False) else root)))}",
            f"数据目录：{_redact(_mask_user(str(default_data_dir())))}",
            f"系统：{platform.platform()}｜Python {platform.python_version()}",
            f"内存：系统 {system.get('percent', '?')}%｜本程序约 {round(own_mb)} MB",
            "",
            "—— 收消息 ——",
            "后台：" + ("在跑" if receiver.get("alive") else "没在跑")
            + (f"（{_redact(receiver.get('detail'))}）" if receiver.get("detail") else ""),
            # 这行是给"点启动→自己退了"这类问题准备的：现场留着这个文件，
            # 就一定是上回点过「停止后台」，一眼定位，不用再翻日志。
            "停止请求文件："
            + ("有 ← 后台一启动就会自己退，删掉 data/stop.request 再试"
               if (default_data_dir() / "stop.request").exists() else "没有"),
            f"配置：{count('group_ids')} 个群 / {count('private_ids')} 个私聊 / "
            f"{count('inbox_ids')} 个收件箱"
            f"｜登录号 {'填了' if int(config.get(qq, 'account', 0) or 0) else '没填'}",
            "",
            "—— 推送 ——",
            "推送：" + ("配好了" if push_ready() else "还没配好"),
            # 每个通道各自的现状（地址只报域名，不含 token —— 这份文字会被贴到公开场合）
            *[f"  {line}" for line in channel_status()],
            f"时间点：{' / '.join(config.get('push', 'times') or []) or '（没设）'}",
        ]
        for row in self.store.recent_pushes(limit=3):
            mark = "✓" if row.get("ok") else "✗"
            lines.append(f"  {mark} {str(row.get('pushed_at') or '')[:16]}｜"
                         f"{_redact(row.get('message') or '')[:60]}")

        lines += ["", "—— 插件 ——"]
        lines += [f"  {item['title']}：{'正常' if item['ok'] else item['error']}"
                  for item in plugins] or ["  （一个都没装）"]

        tail = _tail(_background_log(), 8)
        if tail:
            lines += ["", "—— 后台最近输出 ——"]
            lines += [f"  {_redact(line)}" for line in tail]

        # 界面自己的动作日志：**卡过/出错的话，这一份就是现场**。
        # 加这一节的原因：2026-10-13 窗口"未响应"，从外面什么都查不出来
        # （没对话框、没联网、没在算），因为界面这一侧当时压根不写日志。
        ui_tail = uilog.tail(12)
        if ui_tail:
            lines += ["", "—— 界面最近动作 ——"]
            lines += [f"  {_redact(line)}" for line in ui_tail]

        if include_junk:
            rows = self.store.list_dropped(limit=5)
            if rows:
                # 这一段**故意不打码**：用户勾了它就是要给原文，
                # 打码了反而看不出"为什么这条没收"。所以界面上要写清楚它含什么。
                lines += ["", "—— 最近被拦下的原文（可能含群名和消息内容）——"]
                lines += [f"  [{row.get('reason', '')}] {str(row.get('text') or '')[:80]}"
                          for row in rows]

        text = "\n".join(lines)
        if str(note or "").strip():
            text += "\n\n—— 我想说的 ——\n" + str(note).strip()
        return {"ok": True, "text": text}

    @tracked
    def diagnostics(self, note: str = "") -> dict:
        """打一个诊断包（zip），返回路径。

        和"复制反馈"的区别：反馈是一段文字，只有概况；诊断包里有**结构化的
        事件日志（带完整堆栈）**和脱敏后的配置 —— 用户说"点了没反应"时，
        这份东西才答得上来。
        """
        from ..runtime import diagnostics

        report = self.feedback_report(note)
        plugins = "\n".join(
            f"{item['title']}：{'正常' if item['ok'] else item['error']}"
            for item in (self.plugins().get("items") or []))
        try:
            path = diagnostics.build(report.get("text") or "", plugins=plugins)
        except Exception as exc:
            return {"ok": False, "message": f"打包失败：{type(exc).__name__}: {exc}"}
        return {"ok": True, "path": str(path),
                "message": f"诊断包好了：{path.name}（在 data 文件夹里）"}

    # 暂存文件名长什么样："时间戳-内容指纹-原文件名"。
    # 存成一个固定形状是为了能**严格校验**：界面传回来的 token 直接拼路径，
    # 不校验的话一个 "../.." 就能读到别处的文件。
    DROP_DIR = "drops"
    _DROP_RE = re.compile(r"[0-9]{6,}-[0-9a-f]{10}-[\w.\-]{1,64}")

    def _drop_path(self, token: str) -> Path:
        from ..runtime.config import default_data_dir

        token = str(token or "")
        if not self._DROP_RE.fullmatch(token):
            raise ValueError("这个文件已经处理过了，重新拖一次")
        folder = (default_data_dir() / self.DROP_DIR).resolve()
        path = (folder / token).resolve()
        if folder not in path.parents or not path.is_file():
            raise ValueError("文件不在了，重新拖一次")
        return path
