# 插件放这里

一个插件 = 这一个文件夹里的**一个 `.py` 文件**。

- **装**：把这个文件复制进来，重启私人秘书。
- **卸**：删掉这个文件。
- 文件名以 `_` 开头的会被跳过（留着自己放草稿用）。

插件必须长这样：

```python
from app.contracts.interfaces import DraftEnricher   # 或 TextEnricher

NAME  = "my-plugin"      # 唯一标识
TITLE = "我的插件"        # 界面上显示的名字

class MyEnricher(DraftEnricher):
    name = "my-enricher"
    order = 90           # 数字小的先跑。规则解析是 10，AI 类建议用 90

    def process(self, message, drafts):
        # 对 drafts（list[TaskDraft]）做加工，返回新的列表
        return drafts

def register(registry):
    registry.add_draft(MyEnricher())     # 或 registry.add_text(...)
```

需要**主动去外面拉**的插件（课表订阅、邮箱）注册一个来源，
然后由 `python run.py --sync` 去拉：

```python
from app.contracts.interfaces import Source

class MySource(Source):
    kind  = "my-source"
    title = "我的来源"

    def submit(self, message):   # 别人塞消息给你（外面推的用这个）
        return message

    def fetch(self):             # 主动去拉，返回 list[Message]
        return []

def register(registry):
    registry.add_source(MySource())
```

现成的两个例子：

- `timetable.py` —— **主动去外面拉**（课表：ICS / CSV / JSON 三种格式）；
- `ai.py` —— **加工消息**（把长通知交给 AI 挑待办）。它顺便演示了两件正事：
  怎么调外部 HTTP 接口，以及**失败时怎么退回规则解析**（插件挂了不能拖垮主程序）。

## 写之前先看性能预算（这是硬要求，不是建议）

这个工具的目标是"4 GB 内存的旧电脑也能挂着跑"。所以插件有五条不许：

1. **不许在 import / register 阶段**加载模型、开线程、连网络（插件在启动路径上）；
2. **模型不许进安装包** —— 第一次用时下载，并告诉用户多大；
3. **用完要能卸** —— 常驻的东西（语音、视觉模型）要支持"多久没用就释放"；
4. **闲时不许轮询** —— 界面关了、没消息的时候就该一行代码都不跑；
5. **要下载/要常驻的，必须在说明里写清内存、磁盘、流量**。

规矩和实测数据在 `CONTRACT.md` 的「插件性能预算」那一节。加完插件记得
在那儿补一行实测（后台常驻 ≤ 40 MB、单条消息 ≤ 100 ms、安装包 +≤5 MB）。

**插件写坏了不会影响程序启动**：加载失败只会在这个文件夹对应的状态里
记一笔，界面底部和 `python run.py --selftest` 都会显示出来。

用 `python run.py --plugins` 看当前装了哪些、哪个没加载起来。
