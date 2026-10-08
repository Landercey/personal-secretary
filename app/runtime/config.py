"""配置中心：所有阈值只在这里定义。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  改默认值（过滤强度、保留天数、推送次数…）→ 下面的 DEFAULTS
  加一个新配置项                            → 在 DEFAULTS 里加一行，
                                              然后业务代码用 config.get() 读
  改配置文件长什么样                        → `render_toml()`
  改配置文件存放位置                        → `default_root()` / `default_config_path()`

**铁律**：业务代码里不许出现裸数字。要调行为就改这里，或者改用户的
config.toml。这样以后想调参不用翻代码。

=====================================================================
CONTRACT.md 第五节：代码里不许出现裸数字。要调行为就改这里的 DEFAULTS，
或者改用户目录下的 config.toml。

配置文件用 TOML：
  · 读取用标准库 tomllib（Python 3.11+ 自带，零依赖）；
  · 写回时按段重新生成，注释保留是第 9 步（界面配置面板）的事。
"""

from __future__ import annotations

import os
import time
import tomllib
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 默认值。所有键都带注释说明，改这里等于改行为。
# --------------------------------------------------------------------------
DEFAULTS: dict[str, dict[str, Any]] = {
    "parse": {
        # 过滤强度总开关：strict / loose / off。只给这一个开关，不给十几个细开关。
        "filter_strength": "strict",
        # 低于这个可信度的草稿进废件箱，不推送、不进主列表。
        "confidence_min": 0.45,
        # 同群同人这个秒数内连续发的消息合并成一条待办。
        "merge_window_seconds": 60,
        # 只说了哪天没说几点时的兜底时刻（不写 09:00：一到九点就标红逾期，
        # 但用户说的明明是"今天"）。
        "due_default_hour": "23:59",
    },
    "title": {
        "max_len": 200,
        # 摘掉时间和编号后少于这么多汉字 → 判为碎片。
        # 这个数字是"拿快递""打游戏"和"（A组）""5G"之间的分界线。
        "min_meaningful_chars": 2,
    },
    "store": {
        "attachment_retention_days": 30,
        # 废件箱记录保留多久。删除记录是日后调规则的案例，不许马上丢。
        "keep_dropped_days": 90,
    },
    "push": {
        # 默认一天两次汇总，用户可改。
        "default_times": ["07:30", "21:30"],
        # 推送里带的原文片段长度。
        "quote_max_chars": 80,
        # AI 不可用时的原文保底长度。
        "fallback_text_chars": 300,
    },
    "ai": {
        # conservative = 只挑明确写了要做的事，不重写不推断；aggressive = 允许归纳重写。
        "mode": "conservative",
        "cloud_priority": True,
        "cloud_base_url": "",
        "cloud_api_key": "",
        "cloud_model": "",
        # 0 = 不限制。
        "token_budget_per_day": 0,
        "local_model": "",
    },
    "resource": {
        "mem_critical_percent": 88,
        "mem_resume_percent": 75,
    },
    "risk": {
        "require_secondary_account": True,
        "confirmed_secondary_account": False,
    },
    "wecom_bot": {
        # 群机器人：只要一个 Webhook 地址，不用 CorpID/Secret，也不用配企业可信 IP。
        # 怎么拿：企业微信 → 进一个群（自己建个只有自己的群也行）→ 右上角「…」
        #         → 群机器人 → 添加 → 创建后复制 Webhook 地址，整条贴到下面。
        "enabled": False,
        "webhook": "",                 # ← 用户唯一需要填的一项
        "max_per_minute": 20,          # 官方限制就是 20，改了也没用，留在这里是为了看得见
        "retry_times": 2,              # 网络抖动重试几次
        "timeout": 10,                 # 单次请求超时秒数
        # 推送里要不要带附件（图片原图、文档原件）。手机上看截图靠它。
        "send_attachments": True,
    },
    "qq": {
        # ===== 接入地址 =====
        # NapCat 会主动连到下面这个地址（叫"反向 WebSocket"：由我们监听，
        # 它来连我们）。改了这里，NapCat 那边的配置也要跟着改。
        "enabled": False,                 # 改成 true 才开始收 QQ 消息
        "bind_host": "127.0.0.1",         # 只监听本机，不接受外面的连接
        "bind_port": 18080,
        "path": "/onebot/v11/ws",
        # 首次启用时程序会自动生成一串随机令牌，照抄到 NapCat 配置里。
        # 它的作用是：万一有别的程序也能连本机端口，没有令牌就进不来。
        "token": "",
        # ===== 收哪些消息（默认一个都不收，必须显式勾选）=====
        # 群里闲聊太多，默认不接收是对的：不勾就一条都不进。
        "group_ids": [],                  # 要监控的群号，例如 [123456, 789012]
        "receive_all": False,             # true = 所有群都收（不推荐，噪声和费用都会失控）
        "private_ids": [],                # 要监控的私聊对象 QQ 号
        "receive_private_all": False,
        # 小号收件箱：你自己发给这些会话的消息也会变成待办。
        # 微信/短信截图转发到这里，就等于丢进收件箱。
        "inbox_ids": [],
        "only_mention": False,            # true = 群里只有 @我 的消息才收
        # ===== 安全阀 =====
        "max_text_chars": 3000,           # 单条消息最多留多少字，防超长轰炸
        "keep_every_message": False,      # true = 连闲聊也存进库（排查用，平时关着）
    },
}

# 新建项目必须零真实信息（CONTRACT.md 第十节）。这里只放占位值。
_PLACEHOLDER_KEYS = {"webhook", "cloud_api_key", "cloud_base_url", "cloud_model", "local_model"}

# ---------------------------------------------------------------------------
# 生成给用户看的 config.toml 时，每一项上面带一句人话说明。
# 用户唯一会亲手改的就是这个文件，所以说明写在配置里、而不是只写在代码里。
# 加了新配置项记得在这里补一句 —— 补漏了不会报错，只是用户看不懂那一项。
# ---------------------------------------------------------------------------
KEY_COMMENTS: dict[str, str] = {
    # [parse] 理解层怎么判
    "parse.filter_strength": "过滤强度，三档：strict 严格（默认）/ loose 宽松 / off 关闭。\n"
                             "#   觉得丢太多就改 loose；觉得碎片太多就保持 strict。\n"
                             "#   有人说碎片其实是暗号（学号、房间号），那就改 off。",
    "parse.confidence_min": "可信度低于这个数的待办会进废件箱，不推送。",
    "parse.merge_window_seconds": "同一个人在同一个群里，这个秒数内连续发的东西算一件事。",
    "parse.due_default_hour": "只说了哪天、没说几点时，算当天的几点。默认当天最后一分钟。",
    # [title]
    "title.max_len": "标题最多多少字，超了截断。",
    "title.min_meaningful_chars": "摘掉时间和编号后少于这么多汉字，就当成碎片丢掉。\n"
                                  "#   这是「拿快递」和「（A组）」之间的分界线，\n"
                                  "#   调小了会漏掉真待办，调大了会放进碎片。",
    # [store]
    "store.attachment_retention_days": "图片/文件保留多少天，过期自动删（0 = 永不删）。",
    "store.keep_dropped_days": "废件箱记录保留多少天。这些是调规则的案例，别留太短。",
    # [push]
    "push.default_times": "每天推送的时间点，24 小时制，想推几次就写几个。",
    "push.quote_max_chars": "推送里附带的原文片段长度（方便你看它为什么被判成待办）。",
    "push.fallback_text_chars": "AI 用不了时，推送原文的前多少字作为保底。",
    # [ai]
    "ai.mode": "AI 处理长文档的尺度：conservative 保守（默认，只挑明确要做的事）\n"
               "#   / aggressive 激进（允许重写归纳，读着舒服但可能漏或编）。",
    "ai.cloud_priority": "true = 优先用云端（质量好、要花钱或额度）；false = 优先用本地模型。",
    "ai.cloud_base_url": "云端接口地址。留空 = 不用云端。",
    "ai.cloud_api_key": "云端密钥。⚠️ 这是你的钱包，别把填好的文件发人、别传网盘。",
    "ai.cloud_model": "云端模型名。⚠️ 免费模型名会变，换服务商时记得改这里。",
    "ai.token_budget_per_day": "每天最多用多少 token，0 = 不限制。防止意外烧钱用。",
    "ai.local_model": "本地模型名（要装 Ollama）。留空 = 不用本地模型。",
    # [resource]
    "resource.mem_critical_percent": "内存占用超过这个百分比时，只记待办不分析不推送（别在打游戏时抢资源）。",
    "resource.mem_resume_percent": "内存降到这个百分比以下，恢复正常工作。",
    # [risk]
    "risk.require_secondary_account": "启动时必须确认「我用的是小号」。别关。",
    "risk.confirmed_secondary_account": "你已经确认过了。程序自己写的，不用管。",
    # [wecom_bot]
    "wecom_bot.enabled": "改成 true 才会真的推送。没配好时推送会打印在屏幕上，不会静默丢消息。",
    "wecom_bot.webhook": "企业微信群的 Webhook 地址，整条贴进来。\n"
                         "#   怎么拿：企业微信 → 进一个群（自己建个只有自己的群也行）\n"
                         "#          → 右上角「…」→ 群机器人 → 添加 → 创建完复制地址。",
    "wecom_bot.max_per_minute": "企业微信的硬限制就是每分钟 20 条，改了没用，放这里是为了看得见。",
    "wecom_bot.retry_times": "网络抖动时重试几次。",
    "wecom_bot.timeout": "单次请求等多久算超时（秒）。",
    "wecom_bot.send_attachments": "推送时要不要连图片/文件原件一起发到手机。嫌手机响得勤就改 false。",
    # [qq]
    "qq.enabled": "改成 true 才开始收 QQ 消息。改完要重启程序。",
    "qq.bind_host": "监听地址。默认 127.0.0.1 只接受本机连接，一般不用改。",
    "qq.bind_port": "监听端口。和 NapCat 那边配的端口要一致。",
    "qq.path": "监听路径。和 NapCat 那边配的路径要一致。",
    "qq.token": "访问令牌。首次启用时程序会自动生成一串，照抄到 NapCat 配置里。",
    "qq.group_ids": "要监控的群号，写成 [123456, 789012]。不填就一个群都不收。",
    "qq.receive_all": "true = 所有群都收。不推荐：闲聊噪声多，接 AI 的话费用也会失控。",
    "qq.private_ids": "要监控的私聊对象 QQ 号，写法同上。",
    "qq.receive_private_all": "true = 所有私聊都收。",
    "qq.inbox_ids": "小号收件箱：你自己发给这些会话的消息也变成待办。\n"
                    "#   把微信/短信截图转发到这个会话，就等于丢进收件箱。",
    "qq.only_mention": "true = 群里只有 @我 的消息才收。群太吵可以打开。",
    "qq.max_text_chars": "单条消息最多保留多少字，防超长内容刷屏。",
    "qq.keep_every_message": "true = 连判为闲聊的消息也存进库（排查「为什么没收下」时用）。",
}


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return str(value)


def render_toml(values: dict[str, dict[str, Any]]) -> str:
    """把配置渲染成带逐项说明的 TOML 文本。

    说明来自 KEY_COMMENTS。用户会亲手改这个文件，所以每一项上面都要有
    "这是什么、改了会怎样"，而不是一串看不懂的键名。
    """
    lines = [
        "# 私人秘书 · 用户配置",
        "# 这里只放这台机器自己的设置，不要提交到任何仓库、不要发给别人。",
        "#",
        "# 改完保存就行 —— 绝大多数设置立即生效，不用重启。",
        "# 例外：[qq] 那段改动后要重启（端口在启动时就绑好了）。",
        "",
    ]
    for section in sorted(values):
        lines.append(f"# {'=' * 66}")
        lines.append(f"# [{section}]")
        lines.append(f"# {'=' * 66}")
        lines.append(f"[{section}]")
        for key in sorted(values[section]):
            comment = KEY_COMMENTS.get(f"{section}.{key}")
            if comment:
                for piece in comment.split("\n"):
                    lines.append(piece if piece.lstrip().startswith("#") else f"# {piece}")
            lines.append(f"{key} = {_toml_value(values[section][key])}")
        lines.append("")
    return "\n".join(lines)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for section, values in (override or {}).items():
        if isinstance(values, dict) and isinstance(out.get(section), dict):
            out[section].update(values)
        else:
            out[section] = values
    return out


class Config:
    """读配置。`cfg["parse"]["filter_strength"]` 或 `cfg.get("parse", "filter_strength")`。

    会自动盯着配置文件：每次读取前看一眼文件有没有被改过（最多每 2 秒查一次），
    改了立刻用新的。用户在编辑器里改完保存就能生效，不用重启程序。

    ⚠️ 唯一的例外是 `[qq]` 段：那些值在服务启动时就用来绑端口了，
    改了必须重启。config.toml 的抬头里写明了这一点。
    """

    def __init__(self, values: dict[str, Any] | None = None, path: Path | None = None) -> None:
        self._values = _merge(DEFAULTS, values or {})
        self.path = path
        self._mtime = self._read_mtime()
        self._last_check = 0.0

    # ------------------------------------------------------------------ 热重载
    def _read_mtime(self) -> float:
        if not self.path:
            return 0.0
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def _maybe_reload(self) -> None:
        """每 2 秒最多查一次文件时间，变了就重读。

        不做文件监听（那要引第三方库），轮询足够 ——
        用户改完配置到生效最多差 2 秒，察觉不到。
        """
        if not self.path:
            return
        now = time.monotonic()
        if now - self._last_check < 2.0:
            return
        self._last_check = now
        mtime = self._read_mtime()
        if not mtime or mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            with open(self.path, "rb") as handle:
                fresh = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            # 用户改到一半、语法还没写对时会读失败。保留旧值，等他写完再说 ——
            # 不能因为一次读失败就把正在运行的程序搞乱。
            return
        self._values = _merge(DEFAULTS, fresh)

    # ------------------------------------------------------------------ 读取
    def __getitem__(self, section: str) -> dict[str, Any]:
        self._maybe_reload()
        return self._values.get(section, {})

    def get(self, section: str, key: str, default: Any = None) -> Any:
        self._maybe_reload()
        return self._values.get(section, {}).get(key, default)

    def as_dict(self) -> dict[str, Any]:
        return {k: dict(v) for k, v in self._values.items()}

    # ------------------------------------------------------------------ 加载
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """从文件加载；文件不存在就用默认值（并生成一份带注释的模板）。"""
        target = Path(path) if path else default_config_path()
        values: dict[str, Any] = {}
        if target.is_file():
            try:
                with open(target, "rb") as handle:
                    values = tomllib.load(handle)
            except (OSError, tomllib.TOMLDecodeError):
                # 配置写坏了不能把程序带崩：退回默认值，调用方可以通过
                # cfg.load_error 知道自己写错了。
                values = {}
                cfg = cls(values, target)
                cfg.load_error = f"配置文件读不了（{target.name}），已改用默认值"
                return cfg
        else:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(render_toml(DEFAULTS), encoding="utf-8")
            except OSError:
                pass
        cfg = cls(values, target)
        cfg.load_error = ""
        return cfg

    load_error: str = ""

    def is_placeholder(self, section: str, key: str) -> bool:
        """这个值还是不是占位/空值（用来判断"用户还没配置"）。"""
        if key not in _PLACEHOLDER_KEYS:
            return False
        return not str(self.get(section, key, "") or "").strip()


def default_root() -> Path:
    """程序自己的目录（绿色版：就在程序旁边）。"""
    override = os.environ.get("SECRETARY_HOME")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return default_root() / "config.toml"


def default_data_dir() -> Path:
    return default_root() / "data"


# 全局默认实例。业务代码 import 这个用，别自己 new。
config = Config.load()
