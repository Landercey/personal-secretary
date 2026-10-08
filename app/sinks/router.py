"""输出通道的选择：挑一个当前能用的。

想加一个新通道（钉钉 / 飞书 / 邮件）：
  1. 照抄 sinks/wecom_bot.py 写一个新类，实现 Sink 接口；
  2. 在下面 `build_sink()` 里加一句候选；
  3. 别的代码一行都不用改 —— 这就是契约的意义。
"""

from __future__ import annotations

from ..contracts.interfaces import Sink
from .console import ConsoleSink
from .wecom_bot import WeComBotSink


def candidates() -> list[Sink]:
    """按优先级排好的候选通道。排前面的先用。"""
    return [WeComBotSink.from_config(), ConsoleSink()]


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
