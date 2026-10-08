"""配置中心：所有阈值只在这里定义。

CONTRACT.md 第五节：代码里不许出现裸数字。要调行为就改这里的 DEFAULTS，
或者改用户目录下的 config.toml。

配置文件用 TOML：
  · 读取用标准库 tomllib（Python 3.11+ 自带，零依赖）；
  · 写回时按段重新生成，注释保留是第 9 步（界面配置面板）的事。
"""

from __future__ import annotations

import os
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
        "enabled": False,
        "webhook": "",
        "max_per_minute": 20,
        "timeout": 10,
    },
}

# 新建项目必须零真实信息（CONTRACT.md 第十节）。这里只放占位值。
_PLACEHOLDER_KEYS = {"webhook", "cloud_api_key", "cloud_base_url", "cloud_model", "local_model"}


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return str(value)


def render_toml(values: dict[str, dict[str, Any]]) -> str:
    """把配置渲染成带注释头的 TOML 文本。"""
    lines = [
        "# 私人秘书 · 用户配置",
        "# 改完保存即可生效，不用重启（程序每次读取前会看文件有没有变）。",
        "# 这里只放这台机器自己的设置，不要提交到任何仓库、不要发给别人。",
        "",
    ]
    for section in sorted(values):
        lines.append(f"[{section}]")
        for key in sorted(values[section]):
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
    """读配置。`cfg["parse"]["filter_strength"]` 或 `cfg.get("parse", "filter_strength")`。"""

    def __init__(self, values: dict[str, Any] | None = None, path: Path | None = None) -> None:
        self._values = _merge(DEFAULTS, values or {})
        self.path = path

    # ------------------------------------------------------------------ 读取
    def __getitem__(self, section: str) -> dict[str, Any]:
        return self._values.get(section, {})

    def get(self, section: str, key: str, default: Any = None) -> Any:
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
