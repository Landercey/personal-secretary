"""第四段：输出。把 PushPayload 送出去。

本包内部分工：
  console.py    控制台（开发期验证用，也是最后的兜底通道）
  wecom_bot.py  企业微信群机器人（第 3 步）
  webhook.py    通用推送地址：钉钉 / 飞书 / Bark（第 3 步补的）
  router.py     挑一个当前能用的通道

课表和语音是插件，不在这里。
"""

from .console import ConsoleSink, build_digest, format_task_line
from .router import build_sink, channel_status, push_help_section, push_ready
from .webhook import WebhookSink
from .wecom_bot import WeComBotSink

__all__ = [
    "ConsoleSink",
    "WeComBotSink",
    "WebhookSink",
    "build_sink",
    "channel_status",
    "push_help_section",
    "push_ready",
    "build_digest",
    "format_task_line",
]
