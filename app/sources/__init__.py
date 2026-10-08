"""第一段：来源。把外部世界的东西变成 Message，不做解析。

本包内部分工：
  manual.py  手动录入（粘贴文字、拖入文件）—— 最可靠的兜底入口
  qq.py      QQ 群消息 + 小号收件箱（接 NapCat / OneBot v11 反向 WebSocket）

邮箱是插件（第 10 步之后）。加新来源不用改这个文件以外的任何东西，
只要实现 contracts 里的 Source 接口，然后在 run.py 的 build() 里注册。
"""

from .manual import ManualSource, build_attachment
from .qq import QQSettings, QQSource, ensure_token, event_to_message

__all__ = [
    "ManualSource",
    "build_attachment",
    "QQSource",
    "QQSettings",
    "ensure_token",
    "event_to_message",
]
