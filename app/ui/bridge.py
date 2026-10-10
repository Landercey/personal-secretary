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
import re
import time
from datetime import datetime
from pathlib import Path

from ..contracts.models import STATUS_DONE, STATUS_DROPPED, STATUS_OPEN


def _human_due(due_at: str | None) -> tuple[str, str]:
    """把到期时间变成人话，返回 (文字, 紧急程度)。

    紧急程度给界面用来上色：overdue 红、today 橙、soon 蓝、later 灰、
    none 无时间。
    """
    if not due_at:
        return "没写时间", "none"
    try:
        moment = datetime.strptime(due_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return due_at, "later"

    today = datetime.now().date()
    day = moment.date()
    delta = (day - today).days
    clock = moment.strftime("%H:%M")

    if moment < datetime.now() and delta <= 0:
        return f"已逾期 · {moment.strftime('%m-%d %H:%M')}", "overdue"
    if delta == 0:
        # 当天 23:59 是我们表示"全天"的约定，显示成"今天"更好懂
        return ("今天" if clock == "23:59" else f"今天 {clock}"), "today"
    if delta == 1:
        return ("明天" if clock == "23:59" else f"明天 {clock}"), "soon"
    if delta < 7:
        weekday = "一二三四五六日"[day.weekday()]
        return f"周{weekday}" + ("" if clock == "23:59" else f" {clock}"), "soon"
    return moment.strftime("%m-%d %H:%M"), "later"


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


class Api:
    """网页能调到的全部方法。加方法不用改别的，网页那边直接调。"""

    def __init__(self, store, plugins=(), actions=None) -> None:
        self.store = store
        # 插件加载结果由 run.py 装配时递进来。界面不自己扫文件夹 ——
        # 扫一遍等于把插件又 import 一次，副作用会来两遍。
        self._plugins = list(plugins)
        # 要用到理解层的活（手动录入、拉课表）也由 run.py 装好再递进来。
        # 界面不自己 import 那些模块：全项目只有 run.py 一个装配点。
        self._actions = dict(actions or {})
        # 网页轮询用：每次返回一个自增号，界面上"数据变了没"靠它判断
        self._revision = 0

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
        return {"items": [self._task_dict(t) for t in rows], "revision": self._revision}

    @staticmethod
    def _task_dict(task) -> dict:
        """一条待办 → 界面要的形状。列表、搜索、废件箱共用一个。"""
        due_text, urgency = _human_due(task.due_at)
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
            "remind": int(task.remind_level),
            "attachments": len(task.attachments),
            "files": [{"name": a.name, "path": a.path, "size": int(a.size),
                       "kind": a.kind} for a in task.attachments],
            "status": task.status,
            "done": task.status == STATUS_DONE,
            "archived": bool(task.is_archived),
        }

    def search(self, keyword: str, view: str = "all") -> dict:
        """在当前分组里找。

        **只在当前分组里**（用户报的问题）：在「今天」里搜，已完成/已归档/
        下个月的全冒出来了。搜索框就在这个分组的标题旁边，预期是"筛这个列表"。
        """
        rows = self.store.search(keyword, view=view or "all", limit=200)
        return {"items": [self._task_dict(t) for t in rows],
                "keyword": str(keyword or ""), "view": view or "all"}

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
            "junk": self.store.count_tasks("junk"),
        }

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
        for task in self.store.list_tasks("junk", limit=100):
            items.append({
                "kind": "task", "id": task.task_id, "text": task.title,
                "reason": task.drop_reason or "你标成了「不是待办」",
                "source": task.source_name or "", "at": task.updated_at or "",
            })
        for row in self.store.list_dropped(limit=100):
            items.append({
                "kind": "text", "id": int(row.get("drop_id") or 0),
                "text": str(row.get("text") or ""),
                "reason": str(row.get("reason") or ""),
                "source": str(row.get("source_name") or ""),
                "at": str(row.get("dropped_at") or ""),
            })
        # 谁新谁排前面。两边的字段名不一样，但都是 "YYYY-MM-DD HH:MM:SS"，能直接比。
        items.sort(key=lambda item: item["at"], reverse=True)
        return {"items": items[:200]}

    # ------------------------------------------------------------------ 写
    def complete(self, task_id: int) -> dict:
        self.store.set_status(int(task_id), STATUS_DONE, "界面里标记完成")
        return {"ok": True}

    def drop(self, task_id: int) -> dict:
        """标记误判。留一笔记录，以后调规则用得上。"""
        self.store.set_status(int(task_id), STATUS_DROPPED, "界面里判定为误判")
        return {"ok": True}

    def archive(self, task_id: int) -> dict:
        self.store.archive(int(task_id), "界面里归档")
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
        import base64

        target = self._known_attachment(task_id, path)
        suffix = target.suffix.lower()
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".bmp": "image/bmp", ".webp": "image/webp",
                ".gif": "image/gif"}.get(suffix)
        if not mime:
            return {"ok": True, "image": "", "note": "这种文件没法预览，点「打开」"}
        if target.stat().st_size > 3 * 1024 * 1024:
            return {"ok": True, "image": "", "note": "图太大，点「打开」看原图"}
        blob = base64.b64encode(target.read_bytes()).decode("ascii")
        return {"ok": True, "image": f"data:{mime};base64,{blob}", "note": ""}

    def open_attachment(self, task_id: int, path: str) -> dict:
        """用系统默认程序打开附件。这是用户自己的文件，双击本来也能开。"""
        import os

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
            "qr": qr_info,
            "autostart": desktop.autostart_enabled(),
        }

    def start_qq(self) -> dict:
        """后台起一个 `run.py --serve`（收消息 + 定时推送）。

        为什么开**子进程**而不是在当前进程里起线程：界面关掉不该把收消息
        一起关掉；而且把 WebSocket 服务端和 NapCat 子进程塞进界面进程，
        界面一崩就全崩了。这也是原来的设计（两个进程），只是现在由界面代劳。
        """
        import subprocess
        import sys

        from ..runtime import heartbeat

        if heartbeat.state(self.store)["alive"]:
            return {"ok": False, "message": "后台已经在跑了"}
        project = Path(__file__).resolve().parents[2]
        runner = project / "run.py"
        if not runner.is_file():
            return {"ok": False, "message": f"找不到 {runner}"}
        interpreter = Path(sys.executable).with_name("pythonw.exe")
        exe = interpreter if interpreter.is_file() else Path(sys.executable)
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0))
        try:
            subprocess.Popen(
                [str(exe), str(runner), "--serve"], cwd=str(project),
                creationflags=flags, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            return {"ok": False, "message": f"起不来：{exc}"}
        return {"ok": True, "lazy": True,
                "message": "后台起来了。第一次要等几十秒下载安装 NapCat，"
                           "完了二维码会出现在这里。"}

    def stop_background(self) -> dict:
        """让后台进程自己收工（写个文件，它自己看到就退）。

        为什么不直接杀进程：得先确认那个 PID 确实是我们的后台进程，
        查错了就是误杀别人的程序。写文件让它自己看，简单也安全。
        """
        from ..runtime import heartbeat

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
            "root": str(Path(__file__).resolve().parents[2]),
            "data": str(default_data_dir()),
            "napcat": str(deploy.default_dir()),
            "config": str(config.path or ""),
            "autostart": desktop.autostart_enabled(),
        }

    # ------------------------------------------------------------------ 环境
    def config_summary(self) -> dict:
        """界面上显示"现在什么状态"用。"""
        from ..runtime.config import config
        from ..runtime.heartbeat import state as beat_state
        from ..runtime.sysmon import describe as mem_describe

        receiver = beat_state(self.store)
        return {
            "memory": mem_describe(),
            "qq_groups": len(config.get("qq", "group_ids", []) or []),
            "push_times": config.get("push", "times", []),
            # 界面拖文件时先在前端拦一道，免得白传一遍几十 MB
            "drop_max_mb": int(config.get("ui", "drop_max_mb", 12) or 12),
            # ⚠️ 这是**状态**，不是配置：收消息的进程还在不在。
            # 上面那个 qq_groups 只是"你配了几个群"，跟进程活没活着没关系。
            "receiver": receiver,
            "qq_enabled": bool(config.get("qq", "enabled")),
            "db": str(getattr(self.store, "conn", "")),
        }

    def plugins(self) -> dict:
        """装了哪些插件、哪个坏了。坏的要在界面上看得见（契约第二节）。"""
        return {"items": [
            {"name": p.name, "title": p.title, "ok": p.ok, "error": p.error}
            for p in self._plugins
        ]}

    # ------------------------------------------------------------------ 设置
    def settings(self) -> dict:
        """设置面板要的全部内容：分组 → 段 → 项，带当前值、说明、是否高级、是否锁住。

        清单直接从 config 的默认值生成，所以界面**不会漏项** ——
        以后加了新配置，界面上自动就出现。
        """
        from ..runtime.config import config

        return {"groups": config.schema(), "path": str(config.path or "")}

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

    # ------------------------------------------------------------------ 新手引导
    def setup_state(self) -> dict:
        """还差哪几件事没做。界面拿它决定"新手引导"留不留。"""
        from ..runtime.config import config

        def has(section: str, key: str) -> bool:
            return bool(str(config.get(section, key, "") or "").strip())

        checks = [
            {"key": "risk", "section": "risk", "label": "看过风控提示（小号还是大号）",
             "ok": bool(config.get("risk", "confirmed_secondary_account"))},
            {"key": "wecom", "section": "wecom_bot",
             "label": "填了企业微信机器人的 Webhook",
             "ok": has("wecom_bot", "webhook")},
            {"key": "qq", "section": "qq", "label": "选了要收的群或私聊",
             "ok": bool(config.get("qq", "group_ids") or config.get("qq", "private_ids")
                        or config.get("qq", "inbox_ids")
                        or config.get("qq", "receive_all"))},
            # AI 不在这里列 —— 它**还没做**。
            # 摆一条"配了 AI"等于催用户去填一个不生效的框，纯坑人。
            # 等 AI 插件做出来了，再加一条真正有意义的检查。
        ]
        return {"checks": checks,
                "done": all(item["ok"] for item in checks if not item.get("optional")),
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
    def add_text(self, text: str) -> dict:
        """手动粘贴一条。走的是和 QQ 消息**完全相同**的那条管线。"""
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "message": "先写点什么，再点添加"}
        added = self._act("add_text", text) or 0
        self._revision += 1
        return {"ok": True, "added": added}

    def sync_now(self) -> dict:
        """立刻拉一遍插件来源（课表这类需要主动去拿的）。"""
        result = self._act("sync") or {}
        if not isinstance(result, dict):          # 兜底：老装配点只返回条数
            result = {"added": result, "notes": []}
        self._revision += 1
        return {"ok": True, "added": int(result.get("added") or 0),
                "notes": list(result.get("notes") or [])}

    # ------------------------------------------------------------------ 拖进来的东西
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

    def ingest_drop(self, token: str) -> dict:
        """把暂存的文件**挪进附件目录**再入库。

        为什么先挪：暂存区是临时的，待办要是指着暂存区，哪天清理就把附件清了。
        挪的是同一个盘、同一次改名，不花时间也不占双份空间。
        """
        import shutil

        from ..runtime.config import default_data_dir

        staged = self._drop_path(token)
        path, _note = self._dropped_file(token)
        archive = default_data_dir() / "attachments"
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
        added = self._act("ingest_file", str(target)) or 0
        self._revision += 1
        return {"ok": True, "added": added}

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
