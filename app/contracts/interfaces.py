"""五个接口。实现它们的类不许被别的模块直接 import，只能通过运行时注册表拿。

分成两类理解零件是有原因的（不是过度设计）：
  · TextEnricher 变的是「消息本身」——文件解析、语音转写、AI 读图；
  · DraftEnricher 变的是「草稿」——切分、时间识别、去重、AI 归纳。
硬凑成一个接口，就得在实现里判断"我现在拿到的是消息还是草稿"，更脏。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .models import Message, PushPayload, Task, TaskDraft


class Source(ABC):
    """来源：把外部世界的东西变成 Message，不做任何解析。"""

    kind: str = "source"
    title: str = "未命名来源"

    @abstractmethod
    def submit(self, message: Message) -> Message:
        """外部把消息塞进来（QQ 推送、界面粘贴）。返回归一化后的消息。"""
        raise NotImplementedError

    def fetch(self) -> list[Message]:
        """主动拉取（邮箱用）。默认不拉。"""
        return []


class TextEnricher(ABC):
    """理解层·消息级：只把消息本身变丰富，不产生待办。"""

    name: str = "text-enricher"
    order: int = 50

    def enabled(self) -> bool:
        return True

    @abstractmethod
    def process(self, message: Message) -> Message:
        raise NotImplementedError


class DraftEnricher(ABC):
    """理解层·草稿级：吃消息吐草稿，或对已有草稿做加工。链式执行。"""

    name: str = "draft-enricher"
    order: int = 50

    def enabled(self) -> bool:
        return True

    @abstractmethod
    def process(self, message: Message, drafts: list[TaskDraft]) -> list[TaskDraft]:
        raise NotImplementedError


class Store(ABC):
    """存储：只读写数据库，不放业务规则。"""

    @abstractmethod
    def save_message(self, message: Message) -> None:
        raise NotImplementedError

    @abstractmethod
    def save_drafts(self, message: Message, drafts: list[TaskDraft]) -> list[Task]:
        raise NotImplementedError

    @abstractmethod
    def list_tasks(self, view: str = "open", limit: int = 200) -> list[Task]:
        """view: open / today / tomorrow / week / overdue / archive / junk"""
        raise NotImplementedError

    @abstractmethod
    def set_status(self, task_id: int, status: str, reason: str = "") -> None:
        raise NotImplementedError

    @abstractmethod
    def log_dropped(self, draft: TaskDraft, reason: str) -> None:
        """废件箱 + 删除记录。这些记录就是日后调规则的案例，不许丢。"""
        raise NotImplementedError


@dataclass(slots=True)
class SinkResult:
    ok: bool
    code: int = 0
    message: str = ""


class Sink(ABC):
    """输出：只负责把 PushPayload 送出去，不改内容。"""

    name: str = "sink"

    @abstractmethod
    def deliver(self, payload: PushPayload) -> SinkResult:
        raise NotImplementedError
