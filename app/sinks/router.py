"""输出通道的选择：挑一个当前能用的。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  加一个新通道（邮件 / Telegram…）→ 先写一个 Sink，再往 `candidates()` 里加一条
  改"同时配了好几个走哪个"        → `candidates()` 的先后顺序就是优先级
  改界面左下角那行状态            → `channel_status()` / `push_ready()`
  改"没配好时该跳到设置哪一段"    → `push_help_section()`

想加一个新通道（邮件 / Telegram…）：
  1. 照抄 sinks/wecom_bot.py 写一个新类，实现 Sink 接口；
  2. 在下面 `build_sink()` 里加一句候选；
  3. 别的代码一行都不用改 —— 这就是契约的意义。

钉钉 / 飞书 / Bark 已经由 sinks/webhook.py 一个类全包了
（它们都是"POST 一段 JSON 到你这个地址"，只是报文形状不同，认域名就行）。
"""

from __future__ import annotations

from ..contracts.interfaces import Sink
from .console import ConsoleSink
from .webhook import WebhookSink
from .wecom_bot import WeComBotSink


def candidates() -> list[Sink]:
    """按优先级排好的候选通道。排前面的先用。

    两个都开了的用户走企业微信 —— 只有它能连附件一起发，功能更全。
    """
    return [WeComBotSink.from_config(), WebhookSink.from_config(), ConsoleSink()]


def build_sink() -> Sink:
    """挑第一个能用的通道。都不可用就返回控制台（至少不会静默丢消息）。"""
    options = candidates()
    for sink in options:
        ready = getattr(sink, "ready", None)
        if ready is None or ready():
            return sink
    return options[-1]


def channel_status() -> list[str]:
    """每个通道各自的现状，给自检和设置界面显示用。"""
    lines: list[str] = []
    for sink in candidates():
        describe = getattr(sink, "describe", None)
        lines.append(describe() if describe else f"{sink.name}：可用")
    return lines


def push_ready() -> bool:
    """现在真的能把消息发到手机上吗。

    兜底的 ConsoleSink 不算 —— 它只是"至少不静默丢消息"，手机收不到。
    加通道时不用改这里：挑中的不是控制台就算配好了。
    """
    return getattr(build_sink(), "name", "console") != "console"


def push_help_section() -> str:
    """「推送还没配好」时，界面上那一行点下去该跳到设置的哪一段。

    已经动过企业微信的送回企业微信（多半是漏了那个开关）；
    一动没动的送去「通用推送地址」—— 那一段不用注册企业微信，门槛更低。
    """
    from ..runtime.config import config

    touched = (bool(str(config.get("wecom_bot", "webhook", "") or "").strip())
               or bool(config.get("wecom_bot", "enabled")))
    return "wecom_bot" if touched else "push_webhook"
