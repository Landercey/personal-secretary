# 插件写法（api 1）

一个插件就是 `plugins/` 里的一个 `.py` 文件。主程序每次启动扫描这个文件夹，
挨个执行 `register(registry)`，把你注册的零件接进管线。

**坏插件不会拖垮主程序**：加载失败只会在「插件」面板标红，其余功能照常。

---

## 一、四种零件，选你需要的写

```python
from app.contracts.interfaces import DraftEnricher, TextEnricher, Source

class MyText:            # 消息级：进理解层之前改正文（OCR、转写…）
    name = "my-text"
    order = 50
    def enabled(self): return True
    def process(self, message) -> "Message": ...

class MyDraft:           # 草稿级：切分好之后改/加/删草稿（AI、屏蔽词…）
    name = "my-draft"
    order = 15
    def enabled(self): return True
    def process(self, message, drafts) -> list: ...

class MySource:          # 来源：点「同步」时主动去拉（课表、邮箱…）
    kind = "my-source"
    def fetch(self) -> list: ...

def register(registry):
    registry.add_text(MyText())
    registry.add_draft(MyDraft())
    registry.add_source(MySource())
```

`order` 决定顺序：5 闲聊过滤 / 10 规则解析 / **15 插件一般插在这** / 90 AI。
`process` 里抛异常只会影响这一条消息（主程序会记下完整堆栈），不会崩。

---

## 二、清单（可选，但建议写）

```python
TITLE = "屏蔽词"                 # 界面上显示的名字
DESCRIPTION = "标题里带这些词就不收"
CONFIG_SECTION = "mute"          # 设置面板里"去设置"跳到哪一段

PLUGIN = {
    "api": 1,                    # 你按哪个接口版本写的
    "version": "1.0.0",
    "permissions": [],           # "network" / "files" / "upload"，见下
    "settings": {                # 你自己那一段设置（自动出现在设置里）
        "mute": {
            "title": "屏蔽词",
            "desc": "标题里带这些词就不收",
            "fields": {"words": "砍一刀,拼多多"},     # 键 = 默认值
            "comments": {"words": "屏蔽词（用逗号分开）\n#   第二行起是说明"},
        },
    },
}
```

- **`api`**：主程序只认 ≤ 自己的版本。写大了会被明确拒绝（并告诉你升级主程序）。
  不写 = 按 1 算，所以老插件永远能用。
- **`permissions`**：老实填。用户装插件时会看到它要什么：
  `network`（联网）、`files`（读写文件）、`upload`（把消息内容发出去）。
  什么都不填 = 它只在本机、只碰内存里的数据。
- **`settings`**：声明之后，`config.get("mute", "words", "")` 直接能读，
  界面「设置 → 插件」里自动出现这一段，**不用改前端**。

---

## 三、读设置、写日志

```python
from app.runtime.config import config
from app.runtime import logger

words = str(config.get("mute", "words", "") or "")
logger.note("info", "PLUGIN.LOG", stage="understand",
            detail="我干了什么", plugin="mute-words", msg_id=message.msg_id)
```

`logger.note` 的第四个参数 `stage` 用 `understand` / `output` 这类阶段名；
用户点「保存诊断包」时，这些行会一起进 zip —— 用户报问题时你能看到。

---

## 四、三条红线

1. **`register()` 里只注册**：不联网、不开线程、不加载模型（启动会变慢）。
2. **别偷偷发数据**：要联网就在 `permissions` 里写 `network` / `upload`，
   并在 README 里说清楚发到哪。
3. **别改别人的数据**：只读写你自己那一段配置；库的读写在主程序里，走 `message`/`drafts`。

---

## 五、怎么验

```bash
python run.py --plugins                 # 看它有没有加载成功、报什么错
python run.py --replay qq:123456        # 拿一条真实消息重跑，看你的零件有没有生效
python -m unittest discover -s tests -t .
```

`plugins/_template.py` 是最小骨架，`plugins/mute_words.py` 是带设置和日志的完整例子。
