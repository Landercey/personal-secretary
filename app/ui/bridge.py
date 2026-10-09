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

from ..contracts.models import STATUS_DONE, STATUS_DROPPED


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
              **next_week（下周一~周日）** / overdue / archive / junk
        """
        rows = self.store.list_tasks(view, limit=500)
        items = []
        for task in rows:
            due_text, urgency = _human_due(task.due_at)
            items.append({
                "id": task.task_id,
                "title": task.title,
                "due": due_text,
                "urgency": urgency,
                "location": task.location or "",
                "source": task.source_name or "",
                "sender": task.sender or "",
                "quoted": task.quoted or "",
                "detail": task.detail or "",
                "remind": int(task.remind_level),
                "attachments": len(task.attachments),
                "status": task.status,
            })
        self._revision += 1
        return {"items": items, "revision": self._revision}

    def counts(self) -> dict:
        """左侧导航上的数字。"""
        return {
            "open": self.store.count_tasks("open"),
            "today": self.store.count_tasks("today"),
            "tomorrow": self.store.count_tasks("tomorrow"),
            "week": self.store.count_tasks("week"),
            "next_week": self.store.count_tasks("next_week"),
            "overdue": self.store.count_tasks("overdue"),
            "archive": self.store.count_tasks("archive"),
        }

    def stats(self) -> dict:
        return self.store.stats()

    def dropped(self) -> dict:
        """废件箱：没收下的原文。用来回答"我发的那条为什么没反应"。"""
        return {"items": self.store.list_dropped(limit=200)}

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

    # ------------------------------------------------------------------ 环境
    def config_summary(self) -> dict:
        """界面上显示"现在什么状态"用。"""
        from ..runtime.config import config
        from ..runtime.sysmon import describe as mem_describe

        return {
            "memory": mem_describe(),
            "qq_groups": len(config.get("qq", "group_ids", []) or []),
            "push_times": config.get("push", "times", []),
            # 界面拖文件时先在前端拦一道，免得白传一遍几十 MB
            "drop_max_mb": int(config.get("ui", "drop_max_mb", 12) or 12),
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
            # AI 是选配：不装也能用，所以不算"没做完"
            {"key": "ai", "section": "ai", "optional": True,
             "label": "配了 AI（可选，不填也能用）",
             "ok": has("ai", "cloud_base_url") or has("ai", "local_model")},
        ]
        return {"checks": checks,
                "done": all(item["ok"] for item in checks if not item.get("optional"))}

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
        added = self._act("sync") or 0
        self._revision += 1
        return {"ok": True, "added": added}

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
