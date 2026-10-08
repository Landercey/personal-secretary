"""第一段：来源。把外部世界的东西变成 Message，不做解析。

第 2 步只有手动录入。QQ 群消息、小号收件箱在第 4 步接进来，
邮箱是插件（第 10 步之后）。
"""

from .manual import ManualSource

__all__ = ["ManualSource"]
