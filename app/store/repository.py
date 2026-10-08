"""SQLite 存储实现。

短事务 + WAL：写库不会阻塞前台，也不会在界面卡顿时攒出一堆锁。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
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
from ..runtime.config import config

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def now_text() -> str:
    return datetime.now().strftime(TIME_FORMAT)


def open_database(path: str | Path) -> sqlite3.Connection:
    """打开（必要时初始化）数据库。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def dedup_key_for(draft: TaskDraft) -> str:
    """去重键：同一天 + 同一件事 → 同一条。

    只用标题 + 到期日，不用秒级时间：群里同一件事被转两遍是很常见的，
    但"今天是 10:31 发的、昨天 09:02 又发一遍"不该算两条。
    """
    body = "".join((draft.title or "").split()).lower()
    day = (draft.due_at or "")[:10]
    if not body:
        return ""
    digest = hashlib.sha256(f"{body}|{day}".encode("utf-8")).hexdigest()
    return digest[:32]


class SqliteStore(Store):
    """契约里 `Store` 接口的 SQLite 实现。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ 消息
    def save_message(self, message: Message) -> None:
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
    def save_drafts(self, message: Message, drafts: Iterable[TaskDraft]) -> list[Task]:
        stored: list[Task] = []
        for draft in drafts:
            task = self._insert_draft(message, draft)
            if task is not None:
                stored.append(task)
        return stored

    def _insert_draft(self, message: Message, draft: TaskDraft) -> Task | None:
        key = dedup_key_for(draft)
        if key:
            row = self.conn.execute(
                "SELECT task_id FROM tasks WHERE dedup_key = ?", (key,)
            ).fetchone()
            if row:
                # 同一件事又来一遍：不新增，记一笔事件就够。
                self._event(int(row["task_id"]), "duplicate",
                            f"重复出现（来源 {message.source_name}）")
                return None

        stamp = now_text()
        with self.conn:
            cursor = self.conn.execute(
                """INSERT INTO tasks
                   (title, detail, due_at, location, remind_level, priority,
                    source_kind, source_name, sender, origin_msg_id, confidence,
                    quoted, status, is_archived, dedup_key, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
                (
                    draft.title,
                    draft.detail,
                    draft.due_at,
                    draft.location,
                    int(draft.remind_level),
                    int(draft.priority),
                    draft.source_kind or message.source_kind or SOURCE_MANUAL,
                    draft.source_name or message.source_name,
                    draft.sender or message.sender,
                    draft.origin_msg_id or message.msg_id,
                    float(draft.confidence),
                    draft.quoted,
                    STATUS_OPEN,
                    key,
                    stamp,
                    stamp,
                ),
            )
            task_id = int(cursor.lastrowid or 0)
            for attachment in draft.attachments:
                self._insert_attachment(task_id, message.msg_id, attachment)
            self._event(task_id, "created", f"来自 {draft.source_name or message.source_name}")
        return self.get_task(task_id)

    def _insert_attachment(self, task_id: int, msg_id: str, item: Attachment) -> None:
        self.conn.execute(
            """INSERT INTO attachments
               (task_id, msg_id, name, path, mime, size, kind, sha256)
               VALUES (?,?,?,?,?,?,?,?)""",
            (task_id, msg_id, item.name, item.path, item.mime,
             int(item.size), item.kind, item.sha256),
        )

    def _event(self, task_id: int, kind: str, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO task_events (task_id, kind, detail, created_at) VALUES (?,?,?,?)",
            (task_id, kind, detail, now_text()),
        )

    # ------------------------------------------------------------------ 查询
    def list_tasks(self, view: str = "open", limit: int = 200) -> list[Task]:
        where, params = self._view_filter(view)
        sql = (
            "SELECT * FROM tasks WHERE " + where +
            " ORDER BY (due_at IS NULL), due_at, priority, task_id DESC LIMIT ?"
        )
        rows = self.conn.execute(sql, (*params, int(limit))).fetchall()
        return [self._row_to_task(row) for row in rows]

    def count_tasks(self, view: str = "open") -> int:
        where, params = self._view_filter(view)
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE " + where, params
        ).fetchone()
        return int(row["n"] if row else 0)

    def _view_filter(self, view: str) -> tuple[str, tuple[Any, ...]]:
        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        week_end = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        now = now_text()
        if view == "today":
            return "status='open' AND is_archived=0 AND substr(due_at,1,10)=?", (today,)
        if view == "tomorrow":
            return "status='open' AND is_archived=0 AND substr(due_at,1,10)=?", (tomorrow,)
        if view == "week":
            return ("status='open' AND is_archived=0 AND due_at IS NOT NULL"
                    " AND substr(due_at,1,10) BETWEEN ? AND ?"), (today, week_end)
        if view == "overdue":
            return ("status='open' AND is_archived=0 AND due_at IS NOT NULL AND due_at < ?"),
            (now,)
        if view == "archive":
            return "is_archived=1", ()
        if view == "junk":
            return "status='dropped'", ()
        if view == "all":
            return "1=1", ()
        return "status='open' AND is_archived=0", ()      # open

    def get_task(self, task_id: int) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def list_attachments(self, task_id: int) -> list[Attachment]:
        rows = self.conn.execute(
            "SELECT * FROM attachments WHERE task_id=? ORDER BY attachment_id", (task_id,)
        ).fetchall()
        return [
            Attachment(name=r["name"], path=r["path"], mime=r["mime"],
                       size=int(r["size"]), kind=r["kind"], sha256=r["sha256"])
            for r in rows
        ]

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
    def set_status(self, task_id: int, status: str, reason: str = "") -> None:
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

    def archive(self, task_id: int, reason: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET is_archived=1, archive_reason=?, updated_at=? WHERE task_id=?",
                (reason, now_text(), task_id),
            )
            self._event(task_id, "archived", reason)

    # ------------------------------------------------------------------ 废件箱
    def log_dropped(self, draft: TaskDraft, reason: str) -> None:
        self.log_dropped_text(draft.title, reason, draft.source_kind,
                              draft.source_name, draft.origin_msg_id)

    def log_dropped_text(self, text: str, reason: str, source_kind: str = "",
                         source_name: str = "", msg_id: str = "") -> None:
        """记一条被判为碎片/噪声的原文。翻案、调规则全靠它。"""
        with self.conn:
            self.conn.execute(
                """INSERT INTO dropped_items
                   (text, reason, source_kind, source_name, msg_id, dropped_at)
                   VALUES (?,?,?,?,?,?)""",
                (text[:500], reason, source_kind, source_name, msg_id, now_text()),
            )

    def list_dropped(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM dropped_items ORDER BY drop_id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 维护
    def purge_old_dropped(self) -> int:
        """废件箱记录也要过期清理，否则会一直长。"""
        days = int(config.get("store", "keep_dropped_days", 90))
        cutoff = (datetime.now() - timedelta(days=days)).strftime(TIME_FORMAT)
        with self.conn:
            cursor = self.conn.execute("DELETE FROM dropped_items WHERE dropped_at < ?", (cutoff,))
        return int(cursor.rowcount or 0)

    def stats(self) -> dict[str, int]:
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
