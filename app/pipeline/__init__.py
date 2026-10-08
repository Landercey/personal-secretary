"""第二段：理解。把消息变成待办草稿。

固定顺序（CONTRACT.md 第四节）：
  文件解析 → 时间识别 → 切分 → 去重 → AI 增强（插件）
每一步都能单独关掉，也能被插件替换。

本包内部分工：
  text_norm.py   繁简归一（文字预处理）
  quick_parse.py 规则版解析：时间交给规则，不用 AI
  chain.py       把上面这些零件串成一条管线
"""

from .chain import Pipeline
from .chat_filter import ChatNoiseEnricher

__all__ = ["Pipeline", "ChatNoiseEnricher"]
