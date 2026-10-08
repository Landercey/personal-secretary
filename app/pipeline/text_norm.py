"""文本归一化：繁体 → 简体。

为什么必须有这一步：
  识别规则（"点""明天""周"）全是按简体写的。繁体进来会解析失败 ——
  实测「明天早上11點打遊戲」会被解析成"明天 09:00"（只认出"明天"），时间全错。

zhconv 没装就原样返回，不让这一步成为启动的必要条件。
"""

from __future__ import annotations

try:
    from zhconv import convert as _zh_convert
except ImportError:  # 没装也不影响其它功能
    _zh_convert = None


def to_simplified(text: str) -> str:
    """繁体 → 简体。已经是简体、或没装转换库，就原样返回。"""
    value = str(text or "")
    if not value or _zh_convert is None:
        return value
    try:
        return str(_zh_convert(value, "zh-cn"))
    except Exception:
        return value
