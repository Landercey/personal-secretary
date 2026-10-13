"""四种数据格式：消息 / 待办草稿 / 待办 / 推送内容。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  加一个新的来源类型（比如短信）   → 下面 SOURCE_* 加一个常量，
                                      然后在 sources/ 里写一个实现
  加一个字段（比如"提醒方式"）     → 在对应的 dataclass 里加一行，
                                      然后去 store/schema.sql 加一列，
                                      再在 store/repository.py 的读/写里带上它
  改时间格式                       → TIME_FORMAT（**改这个要想清楚**：
                                      库里已有的数据会读不出来）
  改待办的状态有哪些               → STATUS_* 常量

为什么加字段要动三个地方：契约（这里）、表结构（schema.sql）、读写（repository.py）。
少动一处，字段就会"存进去读不出来"或者"读出来是空的"——这是最容易踩的坑。

=====================================================================
为什么时间全用字符串：
  存库、写日志、发 JSON、跨模块传递都是同一份表示，不会在时区、格式、
  序列化三处打架。格式统一 `"YYYY-MM-DD HH:MM:SS"`。

为什么用 dataclass 不用 pydantic：
  标准库够用，不引第三方依赖 —— 这个项目要能在 4 GB 机器上零依赖跑起来。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 来源类型。加新来源时在这里登记，别在业务代码里写字面量字符串 ——
# 否则以后改名字要全文搜索替换，一定会漏。
# ---------------------------------------------------------------------------
SOURCE_MANUAL = "manual"        # 界面粘贴 / 拖文件进来的
SOURCE_QQ_GROUP = "qq_group"    # QQ 群里别人发的（严格过滤）
SOURCE_QQ_INBOX = "qq_inbox"    # 自己发给小号的（宽松过滤，微信/短信转发走这条）
SOURCE_QQ_PRIVATE = "qq_private"  # 别人私下发给你的（严格过滤）
SOURCE_EMAIL = "email"          # 预留：邮箱插件

# ---------------------------------------------------------------------------
# 附件类型。推送时靠它决定走"图片接口"还是"文件接口"（企微两套接口不一样）。
# ---------------------------------------------------------------------------
KIND_IMAGE = "image"            # 会被当成图片发出去
KIND_DOCUMENT = "document"      # 会被当成文件发出去
KIND_OTHER = "other"

# ---------------------------------------------------------------------------
# 待办状态
# ---------------------------------------------------------------------------
STATUS_OPEN = "open"            # 没做完
STATUS_DONE = "done"            # 用户标了完成
STATUS_DROPPED = "dropped"      # 用户判定"这是误判"，同时成为调规则的案例

# ---------------------------------------------------------------------------
# 时间格式。库里、日志里、推送里必须一模一样，否则排序会乱。
# ---------------------------------------------------------------------------
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def to_dict(obj: Any) -> dict[str, Any]:
    """把契约对象变成可直接入库/发 JSON 的普通字典（嵌套也一起转）。"""
    return asdict(obj)


@dataclass(slots=True)
class Attachment:
    """一个附件。

    去重靠 sha256：同一个文件被转发三次只算一次。
    """

    name: str = ""              # 文件名，给用户看的
    path: str = ""              # 本地绝对路径，推送时用它读文件
    mime: str = ""              # 类型，例如 image/jpeg
    size: int = 0               # 字节数
    kind: str = KIND_OTHER      # image / document / other
    sha256: str = ""            # 内容指纹，去重用

    def exists(self) -> bool:
        from pathlib import Path

        return bool(self.path) and Path(self.path).is_file()


@dataclass(slots=True)
class Message:
    """来源 → 理解。外部世界的一条东西，还没经过任何解析。"""

    msg_id: str                                 # 全局唯一，格式 "来源kind:原始id"
    source_kind: str                            # SOURCE_* 之一
    source_name: str = ""                       # 群名 / 邮箱名 / "手动录入"
    sender: str = ""                            # 发送人昵称或号码
    is_self: bool = False                       # 是不是本机小号自己发的
    sent_at: str = ""                           # 消息时间
    text: str = ""                              # 正文（附件提取的文字会追加进来）
    attachments: list[Attachment] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)   # 原始载荷，只用于排查

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

    title: str                                  # 标题，上限见 config 的 title.max_len
    detail: str = ""                            # 正文/网址/原文，详情面板用
    due_at: str | None = None                   # 截止时间；只说了哪天 → 当天 23:59
    location: str | None = None                 # 地点
    # 提醒力度：**截止前多少分钟**单独提醒一次；0 = 不单独提醒，只在每日汇总里出现。
    # 2026-10-13 从"1 普通 / 2 强提醒 / 3 极重要"三个档改成直接的分钟数 ——
    # 用户要的是"详情里能选 60/30/10/3 分钟前"，一个数字比三个抽象档位直白。
    remind_level: int = 0
    priority: int = 2                           # 调度优先级，数字越小越靠前
    source_kind: str = SOURCE_MANUAL            # 溯源，推送时显示
    source_name: str = ""
    sender: str = ""
    origin_msg_id: str = ""                     # 来自哪条消息
    attachments: list[Attachment] = field(default_factory=list)
    # 可信度 0~1。**现在没有"低于阈值就丢掉"这道闸门**（判值不值得、判碎片
    # 已经各有一道了，见 CONTRACT 第五节）。这个字段留着是给 AI 插件用的：
    # 到时候由它来说"这条我有多确定"。
    confidence: float = 1.0
    quoted: str = ""                            # 原始消息片段，推送时带上


@dataclass(slots=True)
class Task:
    """存储 → 输出。草稿的全部字段 + 生命周期。"""

    task_id: int = 0                            # 数据库主键，界面上的 #编号
    title: str = ""
    detail: str = ""
    due_at: str | None = None
    location: str | None = None
    remind_level: int = 0                       # 同 TaskDraft：提前多少分钟
    priority: int = 2
    source_kind: str = SOURCE_MANUAL
    source_name: str = ""
    sender: str = ""
    origin_msg_id: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    confidence: float = 1.0
    quoted: str = ""
    status: str = STATUS_OPEN                   # open / done / dropped
    is_archived: bool = False                   # 归档的不再出现在主列表
    dedup_key: str = ""                         # 去重键：同一天同一件事算一条
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""
    archive_reason: str = ""                    # 为什么归档（用户手删会写这里）
    drop_reason: str = ""                       # 为什么判成误判


@dataclass(slots=True)
class PushPayload:
    """输出层内部的推送内容。"""

    channel: str                                # 走哪个通道：wecom_bot / webhook / console
    kind: str = "text"                          # text 或 markdown
    title: str = ""
    body: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    note: str = ""                              # 降级/异常说明，会显示给用户
