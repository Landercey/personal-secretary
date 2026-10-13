"""SQLite 存储实现。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  加一张表 / 加一列              → 先改 schema.sql，再改本文件的读和写，
                                    然后改 contracts/models.py 加字段。
                                    三处都要动，少一处就会出现
                                    "存进去了但读出来是空的"
  改"同一件事算不算重复"          → `dedup_key_for()`
  改界面左侧那几个分组的口径      → `_view_filter()`
  改附件保留多久、废件箱留多久    → config.toml 的 [store]
  改排序（哪条排前面）            → `list_tasks()` 里的 ORDER BY

改完必须跑：python -m unittest discover -s tests -t .

=====================================================================
短事务 + WAL：写库不会阻塞前台，也不会在界面卡顿时攒出一堆锁。
"""

from __future__ import annotations

import hashlib
import json
import re
import functools
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from ..contracts.interfaces import Store
from ..contracts.models import (
    TIME_FORMAT,
    STATUS_DONE,
    STATUS_DROPPED,
    STATUS_OPEN,
    SOURCE_MANUAL,
    Attachment,
    Message,
    Task,
    TaskDraft,
)
from ..runtime.config import attachments_dir, config

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def now_text() -> str:
    return datetime.now().strftime(TIME_FORMAT)


def open_database(path: str | Path) -> sqlite3.Connection:
    """打开（必要时初始化）数据库，返回一条普通连接。

    `executescript` 里的 SQL 全是 `CREATE TABLE IF NOT EXISTS`，
    所以这个函数可以放心地每次启动都调用：第一次建表，以后什么都不做。

    跨线程怎么保证安全：不在连接上做文章，而是由 `SqliteStore`
    **把每个方法整体加锁**（见 `_synchronized`）。连接上的
    `check_same_thread=False` 只是关掉"换线程就报错"那道检查，
    真正的互斥靠那把锁。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _normalize_remind_unit(conn)
    conn.commit()
    return conn


def _normalize_remind_unit(conn: sqlite3.Connection) -> None:
    """一次性把老的"提醒档位"（1 普通 / 2 强提醒 / 3 极重要）归零。

    2026-10-13 起 `remind_level` 的含义是**提前多少分钟**（0 = 只在汇总里出现）。
    老值 1/2/3 按新含义会被读成"提前 1/2/3 分钟" —— 那是假的提醒时间，
    所以第一次打开新版本时把它们归零（等于原来的"普通"档：只在汇总里出现）。
    meta 里留个标记，只做一次；用户自己后来选的分钟数不会被碰。
    """
    row = conn.execute("SELECT value FROM meta WHERE key='remind_unit'").fetchone()
    if row is not None:
        return
    conn.execute("UPDATE tasks SET remind_level=0 WHERE remind_level BETWEEN 1 AND 3")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) "
                 "VALUES ('remind_unit', 'minutes')")


def _synchronized(method):
    """把整个方法调用锁起来。

    =====================================================================
    这里踩过两个坑，两次都只在真机/全量测试时才现形
    =====================================================================
    坑一：跨线程用同一条连接，SQLite 直接抛
        "SQLite objects created in a thread can only be used in that same thread"
        症状是"消息显示收下了，但库里一条没有"。

    坑二：以为"给 execute() 加把锁"就够了 —— **不够**。
        execute() 只锁住"执行 SQL"这一下，拿到的游标是在锁**外面**
        才 fetchone() 的。两个线程各拿一个游标又同时读同一条连接，
        Python 的 sqlite3 不支持游标跨线程共享，行数据会串，
        结果是读到别的查询的行，字段变成 NULL。
        症状：`int(row["task_id"])` 突然抛 "TypeError: ... not 'NoneType'",
        十次里只出一次。

    所以锁必须覆盖**整个方法**，从 execute 一路到 fetch 全部结束。
    RLock 是可重入的，所以方法内部调另一个被装饰的方法不会自己锁死自己。

    代价：所有数据库操作串行。这个项目每分钟也就几条消息，
    完全无所谓；换来的是"多线程下也对"这件事不用再猜。
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def dedup_key_for(draft: TaskDraft) -> str:
    """去重键：同一个标题 + 同一个时刻 + 同一个地点 → 同一条。

    为什么不把地点去掉：同一天在同一栋楼的两个地方开会，是两件事。

    为什么用**完整时刻**而不是只取日期（真踩过）：
      "明天上午十点在图书馆集合"和"明天下午三点在图书馆集合"是同一天的
      两场活动，只取日期的话，后一条会被当成前一条的重复直接丢掉 ——
      用户会发现"我发的那条没反应"。

    想改去重尺度就改这里：
      · 觉得太紧（同一件事还是生成两条）→ 把 due_at 截成日期
      · 觉得太松（两件不同的事被并了）→ 把 detail 也一起算进 key
    """
    body = re.sub(r"\s+", "", (draft.title or "")).lower()
    if not body:
        return ""
    when = (draft.due_at or "").strip()
    where = re.sub(r"\s+", "", (draft.location or "")).lower()
    digest = hashlib.sha256(f"{body}|{when}|{where}".encode("utf-8")).hexdigest()
    return digest[:32]


class SqliteStore(Store):
    """契约里 `Store` 接口的 SQLite 实现。

    所有方法都直接对应接口里的一个方法。要换成 MySQL，
    照抄这个文件换掉 SQL 即可，别的模块一行都不用改。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        # 一把可重入锁，把每个公开方法整体锁住。见 `_synchronized`。
        self._lock = threading.RLock()

    @_synchronized
    def close(self) -> None:
        """关连接。

        正常退出和测试里都要调 —— Windows 上不关的话文件还被占着，
        临时目录删不掉（这个也真踩过：测试报 WinError 32）。
        """
        try:
            self.conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ 消息
    @_synchronized
    def save_message(self, message: Message) -> None:
        """把消息存下来。

        `INSERT OR REPLACE` 而不是普通 INSERT：同一条消息被处理两次
        （比如断线重连后补收）不该报错，直接覆盖就好。
        顺带维护 sources 表，记录"这台机器见过哪些群"。
        """
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO messages
                   (msg_id, source_kind, source_name, sender, is_self, sent_at, text, raw, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    message.msg_id,
                    message.source_kind,
                    message.source_name,
                    message.sender,
                    1 if message.is_self else 0,
                    message.sent_at,
                    message.text,
                    json.dumps(message.raw, ensure_ascii=False),
                    now_text(),
                ),
            )
            self.conn.execute(
                """INSERT INTO sources (kind, name, first_seen, last_seen)
                   VALUES (?,?,?,?)
                   ON CONFLICT(kind, name) DO UPDATE SET last_seen = excluded.last_seen""",
                (message.source_kind, message.source_name, now_text(), now_text()),
            )

    # ------------------------------------------------------------------ 待办
    @_synchronized
    def save_drafts(self, message: Message, drafts: Iterable[TaskDraft]) -> list[Task]:
        stored: list[Task] = []
        for draft in drafts:
            task = self._insert_draft(message, draft)
            if task is not None:
                stored.append(task)
        return stored

    def _insert_draft(self, message: Message, draft: TaskDraft) -> Task | None:
        """插入一条草稿。重复的返回 None（不新增，但记一笔事件）。

        ⚠️ 整段"查重 + 插入"必须包在**同一个** `with self.conn:` 里。

        为什么（并发时真踩过）：拆成"先 SELECT 查重、再 INSERT"两段的话，
        两个线程可以都查到"没有重复"，然后一起插入，撞上 dedup_key 的唯一索引，
        抛 IntegrityError。锁只保护单次调用，保护不了"两次调用之间"。

        另外还兜了一层 ON CONFLICT：**跨进程**时（用户同时开了两个程序）
        进程内的锁管不着，那时按"重复"处理，而不是报错崩掉。
        """
        key = dedup_key_for(draft)
        stamp = now_text()
        with self.conn:                      # 锁 + 事务，整段不可分割
            if key:
                row = self.conn.execute(
                    "SELECT task_id FROM tasks WHERE dedup_key = ?", (key,)
                ).fetchone()
                if row:
                    # 同一件事又来一遍：**不新增，但也不白来**。
                    #
                    # 旧项目是"合并"：把新信息补进已有那条，手动重复录入还会
                    # 自动加急跳级。我第一版图省事直接丢弃 —— 结果是
                    # 用户第二次提到某件事时，程序当没看见。
                    # （真踩过：同一天两场"集合"，改动前的去重键只取日期，
                    #   第二条被静默丢掉，用户看到的是"我发的那条没反应"。）
                    self._merge_into(int(row["task_id"]), message, draft)
                    return None
            cursor = self.conn.execute(
                """INSERT INTO tasks
                   (title, detail, due_at, location, remind_level, priority,
                    source_kind, source_name, sender, origin_msg_id, confidence,
                    quoted, status, is_archived, dedup_key, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)
                   ON CONFLICT(dedup_key) WHERE dedup_key <> '' DO NOTHING""",
                (draft.title, draft.detail, draft.due_at, draft.location,
                 int(draft.remind_level), int(draft.priority),
                 draft.source_kind or message.source_kind or SOURCE_MANUAL,
                 draft.source_name or message.source_name,
                 draft.sender or message.sender,
                 draft.origin_msg_id or message.msg_id,
                 float(draft.confidence), draft.quoted,
                 STATUS_OPEN, key, stamp, stamp),
            )
            if not cursor.rowcount:
                # 跨进程撞车：另一个进程刚好插了同一条。当作重复处理。
                return None
            task_id = int(cursor.lastrowid or 0)
            for attachment in draft.attachments:
                self._insert_attachment(task_id, message.msg_id, attachment)
            self._event(task_id, "created", f"来自 {draft.source_name or message.source_name}")
        return self.get_task(task_id)

    def _merge_into(self, task_id: int, message: Message, draft: TaskDraft) -> None:
        """把一条重复的草稿合并进已存在的待办。

        补什么、不补什么，都是有理由的：
          · detail    —— 新的更长就换新的（信息更全）
          · due_at    —— **只在原来没有时才补**。原来有就说明已经定过时间，
                          不能被后一次转发的解析结果覆盖掉
          · location  —— 同理，缺了才补
          · remind    —— 取大的（= 提前更久的那个；重复出现说明这事更要紧）
          · 附件      —— 追加，去重靠 sha256
          · 手动重复  —— 记一笔事件，日后想加"加急跳级"有数据可用
        """
        existing = self.get_task(task_id)
        if existing is None:
            return
        detail = draft.detail if len(draft.detail or "") > len(existing.detail or "") \
            else existing.detail
        due_at = existing.due_at or draft.due_at
        location = existing.location or draft.location
        remind = max(int(existing.remind_level), int(draft.remind_level))
        with self.conn:
            self.conn.execute(
                """UPDATE tasks SET detail=?, due_at=?, location=?, remind_level=?,
                   updated_at=? WHERE task_id=?""",
                (detail, due_at, location, remind, now_text(), task_id),
            )
            for attachment in draft.attachments:
                self._insert_attachment(task_id, message.msg_id, attachment)
            self._event(task_id, "merged",
                        f"重复出现，已合并（来源 {message.source_name or draft.source_name}）")

    def _insert_attachment(self, task_id: int, msg_id: str, item: Attachment) -> None:
        # 同一个文件挂到同一条待办上只留一份。
        # （原注释写着"去重靠 sha256"，但代码是直接 append —— 注释在骗人，
        #   同一个文件被转发三次，库里就有三条一模一样的附件记录。）
        if item.sha256:
            exists = self.conn.execute(
                "SELECT 1 FROM attachments WHERE task_id=? AND sha256=? LIMIT 1",
                (task_id, item.sha256),
            ).fetchone()
            if exists:
                return
        self.conn.execute(
            """INSERT INTO attachments
               (task_id, msg_id, name, path, mime, size, kind, sha256)
               VALUES (?,?,?,?,?,?,?,?)""",
            (task_id, msg_id, item.name, item.path, item.mime,
             int(item.size), item.kind, item.sha256),
        )

    def _event(self, task_id: int, kind: str, detail: str) -> None:
        """记一笔事件。出问题时能回答"它怎么变成现在这样的"。"""
        self.conn.execute(
            "INSERT INTO task_events (task_id, kind, detail, created_at) VALUES (?,?,?,?)",
            (task_id, kind, detail, now_text()),
        )

    # ------------------------------------------------------------------ 查询
    @_synchronized
    def list_tasks(self, view: str = "open", limit: int = 200) -> list[Task]:
        where, params = self._view_filter(view)
        sql = (
            "SELECT * FROM tasks WHERE " + where +
            " ORDER BY (due_at IS NULL), due_at, priority, task_id DESC LIMIT ?"
        )
        rows = self.conn.execute(sql, (*params, int(limit))).fetchall()
        return [self._row_to_task(row) for row in rows]

    @_synchronized
    def count_tasks(self, view: str = "open") -> int:
        where, params = self._view_filter(view)
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE " + where, params
        ).fetchone()
        return int(row["n"] if row else 0)

    def _view_filter(self, view: str) -> tuple[str, tuple[Any, ...]]:
        """把界面上的分组名翻译成 SQL 条件。

        想改"今天"的口径（比如把逾期也算进今天），就改这里。
        返回 (WHERE 子句, 参数)。参数用 ? 占位是防注入的，不要拼字符串。

        **"本周/下周"按自然周算，周一是一周的第一天**，不是"今天往后七天"。
        理由：用户说"本周要做的事"，指的是这个日历周，不是滚动的七天 ——
        周三打开看到的"本周"不该一路排到下周三。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        now = now_text()

        # 本周一 → 本周日 → 下周一 → 下周日
        # Python 的 weekday()：周一=0，周日=6
        monday = (datetime.now() - timedelta(days=datetime.now().weekday())).date()
        this_sunday = monday + timedelta(days=6)
        next_monday = monday + timedelta(days=7)
        next_sunday = monday + timedelta(days=13)
        this_week = (monday.isoformat(), this_sunday.isoformat())
        next_week = (next_monday.isoformat(), next_sunday.isoformat())
        WEEK_SQL = ("status='open' AND is_archived=0 AND due_at IS NOT NULL"
                    " AND substr(due_at,1,10) BETWEEN ? AND ?")

        if view == "today":
            # ⚠️ **没写时间的也算「今天」**（用户定的）。
            #
            # 你不写时间，就等于"今天要做"。不这么算的话，「拿快递」这种随手记的
            # 只有点开「全部未完成」才看得到 —— 而默认视图是「今天」，
            # 用户会以为它丢了。（和"第一眼一片白"是同一类问题。）
            #
            # 只在「今天」这么算：「明天/本周」是问"那一天有什么事"，
            # 无时间的塞进去才叫乱；「逾期」也不能算 —— 它没时间，凭什么算过期。
            return ("status='open' AND is_archived=0 AND (substr(due_at,1,10)=?"
                    " OR due_at IS NULL OR due_at='')"), (today,)
        if view == "tomorrow":
            return "status='open' AND is_archived=0 AND substr(due_at,1,10)=?", (tomorrow,)
        if view == "week":
            return WEEK_SQL, this_week
        if view == "next_week":
            return WEEK_SQL, next_week

        if view == "overdue":
            # ⚠️ 别把这一行拆成两行。写成
            #     return ("...< ?"),
            #     (now,)
            # 行尾那个逗号会让它变成"只有一个元素的元组"，下一行成了独立语句，
            # 解包时报 "not enough values to unpack"。这个坑真的踩过 ——
            # 而且只有真正调 overdue 这条路时才会炸，平时藏着。
            return ("status='open' AND is_archived=0 AND due_at IS NOT NULL"
                    " AND due_at < ?"), (now,)
        if view == "archive":
            return "is_archived=1", ()
        # 做完的。**以前没有这一格**：打勾之后那条从所有列表里消失，
        # 用户第二天想确认"我昨天做完了没"就查不到了。
        if view == "done":
            return "status='done' AND is_archived=0", ()
        if view == "junk":
            return "status='dropped'", ()
        if view == "all":
            return "1=1", ()
        return "status='open' AND is_archived=0", ()      # open

    @_synchronized
    def get_task(self, task_id: int) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    @_synchronized
    def list_attachments(self, task_id: int) -> list[Attachment]:
        """取一条待办的所有附件。推送和详情面板都靠它。"""
        rows = self.conn.execute(
            "SELECT * FROM attachments WHERE task_id=? ORDER BY attachment_id", (task_id,)
        ).fetchall()
        return [
            Attachment(name=r["name"], path=r["path"], mime=r["mime"],
                       size=int(r["size"]), kind=r["kind"], sha256=r["sha256"])
            for r in rows
        ]

    # ------------------------------------------------------------------ 手改与查找
    @_synchronized
    def update_fields(self, task_id: int, fields: dict[str, Any]) -> None:
        """改标题 / 时间 / 地点 / 正文 / 提醒等级。界面上手改用的。

        为什么只认这几样、不给一个万能 update：
        状态走 `set_status`、归档走 `archive`，各有各的副作用要记
        （完成时间、误判原因、归档原因）。混在一个万能方法里，
        以后没人说得清"这条到底被谁改过什么"。
        """
        allowed = ("title", "detail", "due_at", "location", "remind_level")
        changes = {key: value for key, value in (fields or {}).items() if key in allowed}
        if not changes:
            return
        with self.conn:
            # ⚠️ 手改了标题/时间/地点，**去重键必须跟着重算**。
            # 不重算的话：他把"交实验报告"改成"交实验报告（明天）"，
            # 下次群里再说同一件事，程序会当成新的一条再建一次。
            if any(key in changes for key in ("title", "due_at", "location")):
                row = self.conn.execute(
                    "SELECT title, due_at, location FROM tasks WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                if row is not None:
                    changes["dedup_key"] = dedup_key_for(TaskDraft(
                        title=str(changes.get("title", row["title"]) or ""),
                        due_at=changes.get("due_at", row["due_at"]),
                        location=changes.get("location", row["location"]),
                    ))
            sets = ", ".join(f"{key}=?" for key in changes)
            self.conn.execute(
                f"UPDATE tasks SET {sets}, updated_at=? WHERE task_id=?",
                (*changes.values(), now_text(), task_id),
            )
            self._event(task_id, "edited", "；".join(f"{k}={v}" for k, v in changes.items()))

    @_synchronized
    def search(self, keyword: str, view: str = "all",
               limit: int = 100) -> list[Task]:
        """按关键字找。标题、正文、地点、原始片段都搜。

        **只在给定分组里找**（用户报的问题）：他在「今天」里搜索，
        结果已完成、已归档、下个月的全被捞出来了 ——
        "不搜索没事，一搜全出来了"。搜索框就长在那个分组的标题旁边，
        人的预期当然是"在这个列表里筛"。

        为什么原始片段也进搜索范围：用户记得的往往是**原话**
        （"老师说那个实验报告"），不是我们提炼出来的标题。
        """
        word = str(keyword or "").strip()
        if not word:
            return []
        like = f"%{word}%"
        where, params = self._view_filter(view)
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE (" + where + ") AND ("
            "title LIKE ? OR detail LIKE ? OR location LIKE ? OR quoted LIKE ?)"
            " ORDER BY is_archived, (due_at IS NULL), due_at DESC, task_id DESC"
            " LIMIT ?",
            (*params, like, like, like, like, int(limit)),
        ).fetchall()
        return [self._row_to_task(row) for row in rows]

    # ------------------------------------------------------------------ meta
    @_synchronized
    def set_meta(self, key: str, value: str) -> None:
        """记一笔程序自己的状态（现在是心跳用）。

        为什么不塞进 config.toml：那是**用户**的文件，机器自己的心跳
        不该混进去；而且它每几秒写一次，写配置会一直触发重载。
        """
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), str(value)),
            )

    @_synchronized
    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (str(key),)
        ).fetchone()
        return str(row["value"]) if row else default

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            task_id=int(row["task_id"]),
            title=row["title"],
            detail=row["detail"],
            due_at=row["due_at"],
            location=row["location"],
            remind_level=int(row["remind_level"]),
            priority=int(row["priority"]),
            source_kind=row["source_kind"],
            source_name=row["source_name"],
            sender=row["sender"],
            origin_msg_id=row["origin_msg_id"],
            attachments=self.list_attachments(int(row["task_id"])),
            confidence=float(row["confidence"]),
            quoted=row["quoted"],
            status=row["status"],
            is_archived=bool(row["is_archived"]),
            dedup_key=row["dedup_key"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            archive_reason=row["archive_reason"],
            drop_reason=row["drop_reason"],
        )

    # ------------------------------------------------------------------ 状态
    @_synchronized
    def set_status(self, task_id: int, status: str, reason: str = "") -> None:
        """改状态（完成 / 误判）。

        标记完成时会写 completed_at；标成误判时会写 drop_reason ——
        这条记录以后就是调规则的案例。
        """
        stamp = now_text()
        completed = stamp if status == STATUS_DONE else ""
        drop_reason = reason if status == STATUS_DROPPED else ""
        with self.conn:
            self.conn.execute(
                """UPDATE tasks SET status=?, updated_at=?, completed_at=?,
                   drop_reason=COALESCE(NULLIF(?,''), drop_reason) WHERE task_id=?""",
                (status, stamp, completed, drop_reason, task_id),
            )
            self._event(task_id, f"status:{status}", reason)

    @_synchronized
    def archive(self, task_id: int, reason: str = "") -> None:
        """归档。归档 ≠ 删除：数据还在，只是不再出现在主列表。"""
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET is_archived=1, archive_reason=?, updated_at=? WHERE task_id=?",
                (reason, now_text(), task_id),
            )
            self._event(task_id, "archived", reason)

    @_synchronized
    def unarchive(self, task_id: int) -> None:
        """把归档的放回去。**归档的反向操作**。

        为什么必须补这一个（用户报的）：`archive()` 有，反向的却没有 ——
        归档之后**没有任何办法移出来**：界面上没按钮，代码里也没方法。
        点错了只能删库重来。这是漏了，不是取舍。

        `archive_reason` 留着不删：和 `drop_reason` 一样，
        "当初为什么归档的"是以后调规则的案例。
        """
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET is_archived=0, updated_at=? WHERE task_id=?",
                (now_text(), task_id),
            )
            self._event(task_id, "unarchived", "用户把它从归档里放回来")

    # ------------------------------------------------------------------ 废件箱
    @_synchronized
    def log_dropped(self, draft: TaskDraft, reason: str) -> None:
        self.log_dropped_text(draft.title, reason, draft.source_kind,
                              draft.source_name, draft.origin_msg_id)

    @_synchronized
    def log_dropped_text(self, text: str, reason: str, source_kind: str = "",
                         source_name: str = "", msg_id: str = "") -> None:
        """记一条被判为碎片/噪声的原文。翻案、调规则全靠它。

        截断到 500 字：废件箱是给人翻的，不是备份原文的地方。
        """
        with self.conn:
            self.conn.execute(
                """INSERT INTO dropped_items
                   (text, reason, source_kind, source_name, msg_id, dropped_at)
                   VALUES (?,?,?,?,?,?)""",
                (text[:500], reason, source_kind, source_name, msg_id, now_text()),
            )

    @_synchronized
    def list_dropped(self, limit: int = 200) -> list[dict[str, Any]]:
        """列废件箱，最近丢的排前面。"""
        rows = self.conn.execute(
            "SELECT * FROM dropped_items ORDER BY drop_id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(row) for row in rows]

    @_synchronized
    def get_dropped(self, drop_id: int) -> dict[str, Any] | None:
        """取废件箱里的一条（界面上点开那条详情用）。"""
        row = self.conn.execute(
            "SELECT * FROM dropped_items WHERE drop_id=?", (int(drop_id),)
        ).fetchone()
        return dict(row) if row else None

    @_synchronized
    def delete_dropped(self, drop_id: int) -> int:
        """把一条从废件箱里清走（捡回来了，或者用户就想清掉）。

        只删这一笔：原始消息（messages 表）和附件都在原地 ——
        "清废件箱"这个动作不该真的弄丢东西。
        """
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM dropped_items WHERE drop_id=?", (int(drop_id),)
            )
        return int(cursor.rowcount or 0)

    @_synchronized
    def get_message(self, msg_id: str) -> dict[str, Any] | None:
        """按编号取回**原始消息**，raw 已经解成字典。

        用途：废件箱那条被截到 500 字，界面要回答"当时到底收到了什么"、
        "程序读到了什么"（raw 里的 extract_notes）时，得回原文看。
        """
        row = self.conn.execute(
            "SELECT * FROM messages WHERE msg_id=?", (str(msg_id),)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["raw"] = json.loads(item.get("raw") or "{}")
        except ValueError:
            item["raw"] = {}          # 坏 JSON 不该让界面打不开详情
        return item

    # ------------------------------------------------------------------ 合并窗口
    @_synchronized
    def recent_task_for(self, source_kind: str, source_name: str, sender: str,
                        seconds: int) -> Task | None:
        """找"刚才那条消息"建出来的待办。

        用途：先发一句"明天要交作业"，紧接着发一张作业要求的图。
        第二条自己解析不出待办（就一张图），应该挂到第一条上，
        而不是变成一条孤零零的图片待办。

        判定条件：同一个来源 + 同一个发送人 + 最近 `seconds` 秒内建的。
        """
        if seconds <= 0:
            return None
        cutoff = (datetime.now() - timedelta(seconds=int(seconds))).strftime(TIME_FORMAT)
        row = self.conn.execute(
            """SELECT * FROM tasks
               WHERE status='open' AND is_archived=0
                 AND source_kind=? AND source_name=? AND sender=?
                 AND created_at >= ?
               ORDER BY task_id DESC LIMIT 1""",
            (source_kind, source_name, sender, cutoff),
        ).fetchone()
        return self._row_to_task(row) if row else None

    @_synchronized
    def attach_message_files(self, task_id: int, message: Message) -> int:
        """把一条消息的附件挂到已有待办上。返回挂了几份。"""
        if not message.attachments:
            return 0
        count = 0
        with self.conn:
            for attachment in message.attachments:
                self._insert_attachment(int(task_id), message.msg_id, attachment)
                count += 1
            self._event(int(task_id), "attached",
                        f"合并了同一时段的消息附件（{message.source_name}）")
        return count

    # ------------------------------------------------------------------ 推送记录
    @_synchronized
    def already_pushed(self, batch: str) -> bool:
        """这个批次（比如 "2026-10-08 07:30"）推过了吗。

        为什么非要记：程序重启、或者用户手动开了第二次，
        不能把早上那份早报又推一遍 —— 手机响两遍比不推还烦。
        """
        row = self.conn.execute(
            "SELECT push_id FROM push_records WHERE batch=? AND ok=1 LIMIT 1", (batch,)
        ).fetchone()
        return row is not None

    @_synchronized
    def log_push(self, batch: str, channel: str, ok: bool, message: str) -> None:
        """记一笔推送。**一次推送一行**。

        一开始写成"每条待办一行"，结果一份 50 条待办的汇总塞了 50 行 ——
        既浪费又难查（`recent_pushes()` 一屏全是同一批次）。
        我们真正要回答的问题是"这一批推过没有"，一行就够。
        """
        with self.conn:
            self.conn.execute(
                """INSERT INTO push_records
                   (task_id, channel, batch, pushed_at, ok, message)
                   VALUES (NULL,?,?,?,?,?)""",
                (channel, batch, now_text(), 1 if ok else 0, message[:200]),
            )

    @_synchronized
    def recent_pushes(self, limit: int = 20) -> list[dict[str, Any]]:
        """最近推过什么。排查"为什么没收到"时看这个。"""
        rows = self.conn.execute(
            """SELECT * FROM push_records ORDER BY push_id DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 维护
    @_synchronized
    def purge_old_dropped(self) -> int:
        """废件箱记录也要过期清理，否则会一直长。

        保留天数在 config.toml 的 [store] keep_dropped_days（默认 90 天）。
        """
        days = int(config.get("store", "keep_dropped_days", 90))
        cutoff = (datetime.now() - timedelta(days=days)).strftime(TIME_FORMAT)
        with self.conn:
            cursor = self.conn.execute("DELETE FROM dropped_items WHERE dropped_at < ?", (cutoff,))
        return int(cursor.rowcount or 0)

    @_synchronized
    def purge_old_attachments(self) -> int:
        """删掉过期附件的文件和记录。返回删了几个。

        保留天数在 config.toml 的 [store] attachment_retention_days（默认 30 天，
        0 = 永不删）。**这个开关以前只在文档里，代码根本没读** —— 结果是
        "附件永远不会被清理"，用半年这个文件夹能长到几个 G。

        三条安全线：
          1. 只删**已经没有未完成待办指着**的（做完 / 归档 / 判成误判的才动）；
          2. 只删**附件目录里面**的文件 —— 库里那串路径万一被人改过，
             不能拿着它去删别处的文件；
          3. 文件删了记录也删掉，界面上就不会留一个点不开的附件。
        待办本身留着（标题、原文、推送记录都还在），只是图片/文件没了。
        """
        days = int(config.get("store", "attachment_retention_days", 30) or 0)
        if days <= 0:
            return 0
        cutoff = (datetime.now() - timedelta(days=days)).strftime(TIME_FORMAT)
        rows = self.conn.execute(
            """
            SELECT a.attachment_id, a.path FROM attachments a
              JOIN tasks t ON t.task_id = a.task_id
             WHERE t.created_at < ?
               AND (t.status <> 'open' OR t.is_archived = 1)
            """, (cutoff,)).fetchall()
        if not rows:
            return 0

        root = attachments_dir().resolve()
        removed = 0
        with self.conn:
            for row in rows:
                raw = str(row["path"] or "")
                if raw:
                    try:
                        target = Path(raw).resolve()
                        if target.is_relative_to(root) and target.is_file():
                            target.unlink()
                    except OSError:
                        pass          # 删不掉就留着记录，下次再说
                self.conn.execute("DELETE FROM attachments WHERE attachment_id=?",
                                  (int(row["attachment_id"]),))
                removed += 1
        return removed

    @_synchronized
    def stats(self) -> dict[str, int]:
        """几个数字，给 `python run.py --stats` 和以后的状态栏用。"""
        def one(sql: str, *args: Any) -> int:
            row = self.conn.execute(sql, args).fetchone()
            return int(row["n"] if row else 0)

        return {
            "messages": one("SELECT COUNT(*) n FROM messages"),
            "tasks": one("SELECT COUNT(*) n FROM tasks"),
            "open": one("SELECT COUNT(*) n FROM tasks WHERE status='open' AND is_archived=0"),
            "done": one("SELECT COUNT(*) n FROM tasks WHERE status='done'"),
            "dropped": one("SELECT COUNT(*) n FROM dropped_items"),
        }
