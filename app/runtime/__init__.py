"""运行时：配置中心、插件管理、调度、日志。

横跨四个模块，但不属于任何一段流水线。业务模块需要参数时只从 config 读，
不许自己写裸数字 —— 这条是 CONTRACT.md 第五节的红线。
"""

from .config import DEFAULTS, Config, config

__all__ = ["Config", "DEFAULTS", "config"]
