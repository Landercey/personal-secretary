"""五个接口。实现它们的类不许被别的模块直接 import，只能通过运行时注册表拿。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  加一个新来源（邮箱/短信）    → 写一个类继承 Source，放到 sources/ 下
  加一个理解步骤（比如翻译）   → 继承 TextEnricher（改消息）或
                                 DraftEnricher（改草稿），放到 pipeline/ 下
  换存储（比如换成 MySQL）     → 写一个类继承 Store，放到 store/ 下
  加一个推送通道（钉钉）       → 继承 Sink，放到 sinks/ 下，
                                 然后在 sinks/router.py 里登记

**改接口要慎重**：接口一动，所有实现都要跟着改。契约的意义就是让这件事
尽量少发生 —— 优先加一个新接口，而不是往老接口上堆参数。

=====================================================================
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
    """来源：把外部世界的东西变成 Message，不做任何解析。

    解析（这行是不是待办、什么时候到期）是理解层的事。来源只负责
    "把外面的东西原样搬进来"，这样同一个 QQ 接入能同时服务群消息和小号收件箱。
    """

    kind: str = "source"            # 唯一标识，同时会写进 Message.source_kind
    title: str = "未命名来源"        # 设置界面里显示的名字

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
    order: int = 50                 # 数字小的先跑。文件解析 10、AI 读图 90

    def enabled(self) -> bool:
        """False = 这一步跳过。用来做"每步可单独关掉"。"""
        return True

    @abstractmethod
    def process(self, message: Message) -> Message:
        raise NotImplementedError


class DraftEnricher(ABC):
    """理解层·草稿级：吃消息吐草稿，或对已有草稿做加工。链式执行。

    想"否决"这条消息、让后面的零件别跑了，就写
        message.raw["skip_reason"] = "为什么否决"
    然后返回空列表。光返回空列表是不够的 —— 下一个零件会以为"还没轮到它"。
    """

    name: str = "draft-enricher"
    order: int = 50                 # 切分 10 → 时间识别 20 → 去重 30 → AI 90

    def enabled(self) -> bool:
        return True

    @abstractmethod
    def process(self, message: Message, drafts: list[TaskDraft]) -> list[TaskDraft]:
        raise NotImplementedError


class Store(ABC):
    """存储：只读写数据库，不放业务规则。

    业务规则（什么样的行算待办、怎么判重复）全在 pipeline 里。
    这条线一旦模糊，以后换数据库会变成一场灾难。
    """

    @abstractmethod
    def save_message(self, message: Message) -> None:
        raise NotImplementedError

    @abstractmethod
    def save_drafts(self, message: Message, drafts: list[TaskDraft]) -> list[Task]:
        raise NotImplementedError

    @abstractmethod
    def list_tasks(self, view: str = "open", limit: int = 200) -> list[Task]:
        """按视图列待办。

        view 的取值（对应界面左侧那几个分组）：
          open      没做完的
          today     今天到期
          tomorrow  明天到期
          week      本周到期（自然周，周一到周日）
          next_week 下周到期
          overdue   已经过期还没做
          archive   已归档
          junk      废件箱
          all       全部（不过滤）
        """
        raise NotImplementedError

    @abstractmethod
    def set_status(self, task_id: int, status: str, reason: str = "") -> None:
        raise NotImplementedError

    @abstractmethod
    def update_fields(self, task_id: int, fields: dict) -> None:
        """改标题 / 时间 / 地点这些**内容**字段。界面上手改用的。

        和 set_status 分开是有意的：状态变化有副作用要记
        （完成时间、误判原因、归档原因），内容修改没有。
        混成一个万能 update，以后就没人说得清"这条被谁改过什么"。
        """
        raise NotImplementedError

    @abstractmethod
    def search(self, keyword: str, limit: int = 100) -> list[Task]:
        """按关键字找待办。标题、正文、地点、原始片段都要能搜到。"""
        raise NotImplementedError

    @abstractmethod
    def set_meta(self, key: str, value: str) -> None:
        """记一笔程序自己的状态（现在用来存"收消息的进程还活着"的心跳）。

        为什么不写进 config.toml：那是**用户**的文件。
        """
        raise NotImplementedError

    @abstractmethod
    def get_meta(self, key: str, default: str = "") -> str:
        """读 set_meta 存的东西。没有就返回 default。"""
        raise NotImplementedError

    @abstractmethod
    def log_dropped(self, draft: TaskDraft, reason: str) -> None:
        """废件箱 + 删除记录。这些记录就是日后调规则的案例，不许丢。

        为什么强调：用户说"这条不是碎片"时，你得有据可查；
        想把规则调得更准时，也得先有一批真实的误判样本。
        """
        raise NotImplementedError


@dataclass(slots=True)
class SinkResult:
    ok: bool
    code: int = 0
    message: str = ""


class Sink(ABC):
    """输出：只负责把 PushPayload 送出去，不改内容。

    内容怎么组织是上游的事。通道只关心"怎么发"——
    这样加钉钉通道时不用碰任何业务代码。
    """

    name: str = "sink"

    @abstractmethod
    def deliver(self, payload: PushPayload) -> SinkResult:
        raise NotImplementedError
