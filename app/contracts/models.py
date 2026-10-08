"""四种数据格式：消息 / 待办草稿 / 待办 / 推送内容。

为什么时间全用字符串：
  存库、写日志、发 JSON、跨模块传递都是同一份表示，不会在时区、格式、
  序列化三处打架。格式统一 `"YYYY-MM-DD HH:MM:SS"`。

为什么用 dataclass 不用 pydantic：
  标准库够用，不引第三方依赖 —— 这个项目要能在 4 GB 机器上零依赖跑起来。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# 来源类型。加新来源时在这里登记，别在业务代码里写字面量。
SOURCE_MANUAL = "manual"
SOURCE_QQ_GROUP = "qq_group"
SOURCE_QQ_INBOX = "qq_inbox"
SOURCE_EMAIL = "email"

# 附件类型
KIND_IMAGE = "image"
KIND_DOCUMENT = "document"
KIND_OTHER = "other"

# 待办状态
STATUS_OPEN = "open"
STATUS_DONE = "done"
STATUS_DROPPED = "dropped"

# 时间格式
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def to_dict(obj: Any) -> dict[str, Any]:
    """把契约对象变成可直接入库/发 JSON 的普通字典（嵌套也一起转）。"""
    return asdict(obj)


@dataclass(slots=True)
class Attachment:
    """一个附件。sha256 用来做内容层去重。"""

    name: str = ""
    path: str = ""
    mime: str = ""
    size: int = 0
    kind: str = KIND_OTHER
    sha256: str = ""

    def exists(self) -> bool:
        from pathlib import Path

        return bool(self.path) and Path(self.path).is_file()


@dataclass(slots=True)
class Message:
    """来源 → 理解。外部世界的一条东西，还没经过任何解析。"""

    msg_id: str
    source_kind: str
    source_name: str = ""
    sender: str = ""
    is_self: bool = False
    sent_at: str = ""
    text: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def quote(self, limit: int) -> str:
        """原始片段，推送里带上，让用户看得出为什么被判成待办。"""
        body = " ".join(str(self.text or "").split())
        if not body and self.attachments:
            body = "［附件：%s］" % self.attachments[0].name
        if len(body) <= limit:
            return body
        return body[:limit] + "…"


@dataclass(slots=True)
class TaskDraft:
    """理解 → 存储。一行文字被解析成的一件事。"""

    title: str
    detail: str = ""
    due_at: str | None = None
    location: str | None = None
    remind_level: int = 1
    priority: int = 2
    source_kind: str = SOURCE_MANUAL
    source_name: str = ""
    sender: str = ""
    origin_msg_id: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    confidence: float = 1.0
    quoted: str = ""


@dataclass(slots=True)
class Task:
    """存储 → 输出。草稿的全部字段 + 生命周期。"""

    task_id: int = 0
    title: str = ""
    detail: str = ""
    due_at: str | None = None
    location: str | None = None
    remind_level: int = 1
    priority: int = 2
    source_kind: str = SOURCE_MANUAL
    source_name: str = ""
    sender: str = ""
    origin_msg_id: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    confidence: float = 1.0
    quoted: str = ""
    status: str = STATUS_OPEN
    is_archived: bool = False
    dedup_key: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""
    archive_reason: str = ""
    drop_reason: str = ""


@dataclass(slots=True)
class PushPayload:
    """输出层内部的推送内容。"""

    channel: str
    kind: str = "text"
    title: str = ""
    body: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    note: str = ""
