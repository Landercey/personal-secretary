# 写一个插件

一个插件 = 这个文件夹里的**一个 `.py` 文件**。没有构建、没有依赖清单、没有注册中心：
丢进来、重启程序就是装好；删掉文件就是卸载。

**完整接口文档**：[PLUGIN_API.md](PLUGIN_API.md)（清单、权限、自己那一段设置、日志、三条红线）。
**两个现成例子**：`_template.py`（最小骨架）、`mute_words.py`（带设置和日志）。

- **别用 `_` 开头命名** —— 加载器会跳过这种文件（那是留给你放草稿的）。
  想抄模板：把 `_template.py` 复制成 `my_plugin.py` 再改。
- **想给别人用**：那一个 `.py` 直接发给他，他拖进程序窗口 → 右边弹「装成插件」。
  （README 的「插件」那一节有截图和说明。）
- **文件名 = 模块名**：别跟内置的 `ai.py` / `timetable.py` / `mail.py` 重名。

---

## 三种插件，挑一种

| 你想干的事 | 继承 | 注册方式 | 现成例子 |
|---|---|---|---|
| 把消息**本身**变丰富（读附件、抽字段） | `TextEnricher` | `registry.add_text(...)` | 核心零件就是这个形状（`app/pipeline/extract/`） |
| 从草稿里**挑/改待办**（决定"这算不算一件事"） | `DraftEnricher` | `registry.add_draft(...)` | `ai.py`（长通知交给模型） |
| **主动去外面拉**（邮箱、订阅、接口） | `Source` | `registry.add_source(...)` | `timetable.py`、`mail.py` |

跑的顺序：TextEnricher 先跑 → 规则解析 → DraftEnricher 按 `order` 从小到大（AI 用 90）。
你只是"加工"，别自己判断时间格式、别自己写库 —— 那些有现成的零件。

## 最少要写的三样

```python
NAME  = "my-plugin"      # 唯一标识；别跟内置的 ai / timetable / mail 撞
TITLE = "我的插件"        # 界面上显示的名字

def register(registry):  # 装配时被调用一次（**只干注册，别在这里联网/加载模型**）
    registry.add_draft(MyEnricher())
```

可选但推荐：`DESCRIPTION`（一句话说清干什么，界面会显示）、
`CONFIG_SECTION`（如果你用的是核心已有的配置段，比如 AI 用 `ai`）。

## import 的规矩（最容易翻车的一条）

程序是打包成 exe 发给别人的，**只有打包时列进白名单的东西才在包里**：

- ✅ **标准库**基本都能用：`json` / `re` / `datetime` / `urllib` / `sqlite3` / `hashlib`…
- ⚠️ **有些标准库没打进去**（邮箱插件要的 `imaplib` 就是）。用了就在
  `tools/build_exe.py` 的 `HIDDEN_IMPORTS` 里补一行 ——
  否则**源码运行完全正常，打包版一加载就 `ModuleNotFoundError`**（2026-10-14 真踩过）；
- ❌ **第三方库用不了**（requests / pandas / torch 都不在包里，也被明确排除）。
  要联网就用标准库的 `urllib`（`ai.py` 就是这么写的）；想用第三方库只能自己 fork 重打包。

## 配置怎么给用户

**插件不能往设置面板里加字段** —— 那个面板是照核心的 `app/runtime/config.py` 生成的。
两条路：

1. **借用核心已有的段**：在插件里写 `CONFIG_SECTION = "ai"` 之类，用户就能在界面上配
   （AI 插件就是这么干的）；
2. **自己定一个段**，让用户手改 `config.toml`：

```toml
[my_plugin]
api_url = "https://..."
```

```python
from app.runtime.config import config

url = config.get("my_plugin", "api_url", "")   # 读得到；段不存在也给默认值
```

**读是安全的，写不行**（`config.set` 只认核心认识的那些项）—— 插件不该偷偷改用户的配置。
要在界面上给你自己的字段做配置页，目前得去改核心（可以提 issue 说一声）。

## 六条硬规矩（不及格就别发）

1. **不许在 import / register 阶段**联网、加载模型、开线程 —— 你在启动路径上，
   等你的每一毫秒都是用户的耐心；
2. **不许把模型塞进安装包**：要下载就第一次用时下，并告诉用户多大；
3. **用完要能卸**：常驻的东西（语音、视觉模型）要能"多久没用就释放"；
4. **闲时一行代码都不跑**：不许轮询；要"拉"就做成 `Source`，由用户点「同步」触发；
5. **出错不许抛给主程序**：`fetch()` 里自己接住，用 `self.notes.append("人话")` 说出来；
   加工类的插件挂了要**退回原来的结果**（`ai.py` 演示了这件事），
   绝不能因为你的插件让用户"什么都没收到"；
6. **装包体积**：每个插件 ≤ +5 MB。体积/内存/耗时写进 `CONTRACT.md` 的
   「插件性能预算」那张表，量法也在那儿。

## 怎么测、怎么发

```bash
python run.py --plugins      # 加载起来了吗？坏在哪儿？
python run.py --sync         # 需要"拉"的插件：真跑一次（用假服务器测，别拿真账号）
python -m unittest tests.test_mail_plugin    # 照着抄这份：不连真邮箱也能测
python tools/build_exe.py    # 发之前必跑：它会用**打包版**再自检一次
```

`tools/build_exe.py` 那一步很关键：**插件 import 不到东西，只有打包版才暴露**。
跑绿了再发。

## 加完插件记得做三件事

1. `plugins/README.md`（就是这个文件）里把例子补上；
2. `CONTRACT.md` 的「插件性能预算」表里加一行**实测**；
3. 如果它要用户填配置，`config.toml` 的注释里写清怎么填（`config.py` 的 `KEY_COMMENTS`）。

**插件写坏了不会影响程序启动**：加载失败只在插件那一栏里记一笔，
界面底部和 `python run.py --selftest` 都会显示出来。
