"""第四段：输出。把 PushPayload 送出去。

第 2 步只有控制台；企微群机器人在第 3 步接进来；
课表和语音是插件。
"""

from .console import ConsoleSink

__all__ = ["ConsoleSink"]
