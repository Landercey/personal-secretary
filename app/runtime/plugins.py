"""插件系统：扫描 `plugins/`，把插件里的零件注册进管线。

=====================================================================
为什么"一个插件 = 一个 .py 文件"
=====================================================================
目标用户是不装 Python、不碰命令行的人。安装步骤每多一步，流失一半人。
所以：

    装  = 把一个 .py 丢进 plugins/ 文件夹
    卸  = 删掉那个文件

没有清单文件、没有安装器、没有版本号、没有依赖解析 —— 这些都要用户
理解一堆概念，换来的好处本项目根本用不上。

=====================================================================
插件长什么样
=====================================================================
    NAME  = "my-plugin"         # 唯一标识
    TITLE = "我的插件"           # 界面上显示的名字，可省（默认用文件名）

    def register(registry):
        registry.add_draft(MyEnricher())    # 或 registry.add_text(...)
        # 需要主动去外面拉的（课表订阅、邮箱）用 registry.add_source(...)

文件名以 `_` 开头的会被跳过（留着自己放草稿用）。

**插件的代码能 import 项目里的 `app.contracts`**，因为 run.py 已经把
项目根目录放进了 sys.path。除此之外不许 import 别的模块的内部文件。

=====================================================================
铁律：坏插件不许拖垮主程序（CONTRACT.md 第二节）
=====================================================================
用户在网上下到一个写坏的插件，程序必须**照常启动**，并在界面上说清楚
是哪个插件坏了、坏在哪。所以这里三层全包：

  1. 导入就炸（语法错、import 了不存在的东西）→ 记一笔，继续下一个
  2. `register()` 跑到一半抛异常                    → 记一笔，继续下一个
  3. 注册进来的东西根本不是零件（没继承接口）      → 记一笔，继续下一个

第 2、3 条必须**回滚**：注册到一半崩了，注册表里已经躺进去的半个插件
会进管线，变成"看着加载失败、其实还在跑"的鬼东西。
回滚靠"记下注册前的长度，出事就截回去"实现。

插件在**运行时**出错（process() 抛异常）不归这里管 ——
`pipeline/chain.py` 已经逐零件 try/except 了，那是另一层。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..contracts.interfaces import DraftEnricher, Source, TextEnricher

PLUGIN_DIRNAME = "plugins"


@dataclass(slots=True)
class PluginStatus:
    """一个插件的加载结果。界面上就靠它说话。"""

    name: str                    # 文件名（不含 .py）
    title: str                   # 插件自己声明的显示名
    ok: bool
    error: str = ""              # 失败原因，说人话
    # 插件可以自己声明"我的设置在设置面板的哪一段"，界面就能给一个「去设置」。
    # 不声明也没有：那就只有名字和状态。
    section: str = ""
    description: str = ""        # 一句话说它是干什么的

    def describe(self) -> str:
        if self.ok:
            return f"{self.title}（{self.name}）：正常"
        return f"{self.title}（{self.name}）：加载失败 — {self.error}"


@dataclass
class Registry:
    """插件往这里塞零件，run.py 的 build() 再把它们倒进管线。

    三个槽：理解层的消息级/草稿级零件，加上来源（课表订阅、邮箱这类
    需要主动去拉的）。

    shortcut: 还没有输出通道的槽（推送插件）。等真有那个插件时再加 ——
              现在加上去只是一个没人读的空列表。
    """

    text_enrichers: list = field(default_factory=list)
    draft_enrichers: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    # load() 会把每个插件的加载结果放这里，界面和自检都读它
    status: list["PluginStatus"] = field(default_factory=list)

    def add_text(self, enricher) -> None:
        """注册一个消息级零件（AI 读图、语音转写…）。"""
        self.text_enrichers.append(enricher)

    def add_draft(self, enricher) -> None:
        """注册一个草稿级零件（AI 归纳、插件版切分…）。"""
        self.draft_enrichers.append(enricher)

    def add_source(self, source) -> None:
        """注册一个来源（课表订阅、邮箱…）。由 run.py --sync 主动去拉。"""
        self.sources.append(source)


def plugin_dir(root: Path) -> Path:
    """插件文件夹。用户建不建都行，不存在就当没有插件。"""
    return Path(root) / PLUGIN_DIRNAME


def discover(root: Path) -> list[Path]:
    """找出所有插件文件。返回排好序的路径，结果稳定好排查。"""
    folder = plugin_dir(root)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.py") if not p.name.startswith("_"))


def seed(root: Path, source: Path) -> int:
    """把**随包发的**插件补进用户自己的 plugins/，只补缺、不覆盖。

    为什么要补：打包成 exe 之后，程序自己的文件都收在 `_internal` 里（用户看不见），
    而插件必须是"看得见、改得动、删得掉"的一个 .py —— 自带的课表插件
    得在他那份 plugins/ 里也放一份。

    **只补缺**：他已经改过的、或者故意删掉的，重开一次不能被我们塞回去。
    返回补了几个。
    """
    import shutil

    if not source.is_dir():
        return 0
    target_dir = plugin_dir(root)
    target_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for item in source.iterdir():
        if not item.is_file() or item.suffix != ".py":
            continue
        target = target_dir / item.name
        if target.exists():
            continue
        shutil.copy2(item, target)
        copied += 1
    return copied


def load(root: Path, registry: Registry) -> list[PluginStatus]:
    """把 `plugins/` 下的插件全加载一遍。**这个函数不会抛异常。**

    返回每个插件的结果，成功的和失败的都在里面 ——
    "没有插件"和"插件全挂了"在界面上要能分开，所以不能只返回成功的。

    结果同时记在 `registry.status` 上，免得调用方再传一个列表到处走。
    """
    registry.status = [_load_one(path, registry) for path in discover(root)]
    return registry.status


def _module_name(stem: str) -> str:
    """文件名 → 能当模块名用的字符串（`course-table` → `course_table`）。"""
    return "ps_plugin_" + re.sub(r"\W", "_", stem)


def _load_one(path: Path, registry: Registry) -> PluginStatus:
    name = path.stem
    title = name
    section = ""
    description = ""
    # 注册前的长度。出任何事都截回这里，不留半个插件在管线里。
    text_before = len(registry.text_enrichers)
    draft_before = len(registry.draft_enrichers)
    source_before = len(registry.sources)

    try:
        spec = importlib.util.spec_from_file_location(_module_name(name), path)
        if spec is None or spec.loader is None:
            raise ImportError("这个文件没法当成 Python 模块加载")
        module = importlib.util.module_from_spec(spec)
        # 必须先登记到 sys.modules 再执行：`@dataclass(slots=True)` 会回头
        # 去 sys.modules 里找自己的模块，没登记就直接 AttributeError。
        # （真踩过：课表插件用了个 dataclass，加载报"NoneType has no __dict__"。）
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(spec.name, None)
            raise

        title = str(getattr(module, "TITLE", "") or name)
        section = str(getattr(module, "CONFIG_SECTION", "") or "")
        description = str(getattr(module, "DESCRIPTION", "") or "")
        register = getattr(module, "register", None)
        if not callable(register):
            raise AttributeError("插件里没写 register(registry) 函数")
        register(registry)

        # 注册进来的是不是真的零件？不是就整条回滚。
        _verify(registry, text_before, draft_before, source_before)
    except Exception as exc:
        del registry.text_enrichers[text_before:]
        del registry.draft_enrichers[draft_before:]
        del registry.sources[source_before:]
        return PluginStatus(name, title, False, f"{type(exc).__name__}: {exc}",
                            section, description)

    return PluginStatus(name, title, True, "", section, description)


def _verify(registry: Registry, text_before: int, draft_before: int,
            source_before: int) -> None:
    """检查这个插件刚注册进来的零件合不合规矩（不合规就抛异常触发回滚）。

    为什么要在加载时就卡：管线是按 `order` 排序的，
    一个没有 `order` 的东西会**在程序启动那一刻**把排序炸掉 ——
    那就变成了"坏插件拖垮主程序"，正好违反铁律。
    """
    for part in registry.text_enrichers[text_before:]:
        if not isinstance(part, TextEnricher):
            raise TypeError(f"{type(part).__name__} 不是 TextEnricher（要用 registry.add_text）")
    for part in registry.draft_enrichers[draft_before:]:
        if not isinstance(part, DraftEnricher):
            raise TypeError(f"{type(part).__name__} 不是 DraftEnricher（要用 registry.add_draft）")
    for part in registry.sources[source_before:]:
        if not isinstance(part, Source):
            raise TypeError(f"{type(part).__name__} 不是 Source（要用 registry.add_source）")
