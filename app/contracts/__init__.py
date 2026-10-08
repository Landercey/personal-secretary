"""契约层：模块之间唯一允许交换的数据格式与接口。

规矩（CONTRACT.md 第二节）：
  · 四个模块之间只准传本包定义的格式；
  · 别的模块不许 import 这个包以外的内部实现。
"""

from .models import Attachment, Message, PushPayload, Task, TaskDraft
from .interfaces import (
    DraftEnricher,
    Sink,
    SinkResult,
    Source,
    Store,
    TextEnricher,
)

__all__ = [
    "Attachment",
    "Message",
    "PushPayload",
    "Task",
    "TaskDraft",
    "DraftEnricher",
    "Sink",
    "SinkResult",
    "Source",
    "Store",
    "TextEnricher",
]
