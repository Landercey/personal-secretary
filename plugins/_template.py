"""插件模板 —— 复制这个文件，改个名字（别以 `_` 开头），再改内容。

文件名以 `_` 开头的会被加载器**跳过**，所以这个模板放在这儿不会被执行。
复制成 `my_plugin.py` 之后它就生效了。

三种插件挑一种就够了，用不上的那两段删掉。
完整规矩（import 白名单、配置怎么写、性能红线）见同目录 README.md。
"""

import hashlib
import urllib.request

from app.contracts.interfaces import DraftEnricher, Source        # 按需保留
from app.contracts.models import Message, TaskDraft
from app.runtime.config import config

NAME = "my-plugin"          # 唯一标识；别跟 ai / timetable / mail 撞
TITLE = "我的插件"           # 界面上显示的名字
DESCRIPTION = "一句话说清它干什么（界面会显示）"

# 清单（可选，但建议写）：版本、权限、以及**你自己那一段设置**。
# 写了 settings 之后，设置面板「插件」组里会自动出现这一段，不用改前端。
# 权限老实填：network（联网）/ files（读写文件）/ upload（把消息内容发出去）。
PLUGIN = {
    "api": 1,                       # 按哪个接口版本写的；不写按 1 算
    "version": "0.1.0",
    "permissions": [],
    # "settings": {
    #     "myopt": {
    #         "title": "我的插件设置",
    #         "desc": "一句话说明",
    #         "fields": {"words": ""},           # 键 = 默认值
    #         "comments": {"words": "标签\n#   第二行起是说明"},
    #     },
    # },
}


# ---------------------------------------------------------------- 写法 A：加工待办
# 什么时候用：你想在"规则挑完之后"再挑一遍、或者把草稿改一改。
# 例子：ai.py（把长通知交给模型）。order 越大越晚跑（规则是 10，AI 是 90）。
class MyEnricher(DraftEnricher):
    name = "my-enricher"
    order = 95

    def process(self, message: Message, drafts: list[TaskDraft]) -> list[TaskDraft]:
        # message.text 是正文（附件里读出来的字已经拼进去了）
        # drafts 是规则已经挑出来的待办；返回新的列表，就等于替换掉它们。
        # ⚠️ 自己出错时**必须退回 drafts**，不能让用户"什么都没收到"。
        try:
            if "某个关键词" not in message.text:
                return drafts
            return drafts + [TaskDraft(title="我加的这条", due_at=None,
                                       source_kind=message.source_kind,
                                       source_name=message.source_name,
                                       sender=message.sender,
                                       origin_msg_id=message.msg_id,
                                       quoted=message.quote(80))]
        except Exception:                                   # noqa: BLE001
            return drafts                                     # 出错就当作没我


# ---------------------------------------------------------------- 写法 B：主动去拉
# 什么时候用：东西不在消息里，得你去外面取（邮件、订阅、某个接口）。
# 例子：timetable.py、mail.py。**只在用户点「同步」时被调用一次** —— 别自己轮询。
class MySource(Source):
    kind = "my-source"        # 会写进 Message.source_kind
    title = TITLE

    def __init__(self) -> None:
        # 有话要跟用户说（缺配置、被限流…）就往这里塞，界面会显示出来
        self.notes: list[str] = []

    def submit(self, message: Message) -> Message:
        return message        # 只拉不推的来源照抄这一行

    def fetch(self) -> list[Message]:
        self.notes = []
        if not config.get("my_plugin", "enabled", False):
            return []
        api_url = str(config.get("my_plugin", "api_url", "") or "").strip()
        if not api_url:
            # 缺东西别静默、也别抛异常：说清缺哪一项、去哪儿填
            self.notes.append("还没填 api_url —— 去「设置」里补上，再点一次同步。")
            return []
        out: list[Message] = []
        try:
            # 联网用标准库 urllib（第三方库不在打包版里，见 README）
            with urllib.request.urlopen(api_url, timeout=15) as response:
                lines = response.read().decode("utf-8", "replace").splitlines()
        except Exception as exc:                            # noqa: BLE001
            self.notes.append(f"拉取失败：{type(exc).__name__}: {exc}")
            return []
        for index, line in enumerate(line for line in lines if line.strip()):
            # msg_id 必须**稳定**：重复拉到同一条时靠它去重，不会变成两条待办。
            # 所以按内容算指纹，而不是按行号（行号会随列表变化而漂）。
            digest = hashlib.sha1(line.strip().encode("utf-8")).hexdigest()[:12]
            out.append(Message(msg_id=f"my-source:{digest}", source_kind=self.kind,
                               source_name="我的来源", text=line.strip()))
        return out


def register(registry) -> None:
    """装配时被调用一次。**只准注册，别在这里联网/加载模型/开线程。**"""
    registry.add_draft(MyEnricher())      # 写法 A
    # registry.add_source(MySource())     # 写法 B
