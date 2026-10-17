"""诊断包：把"查一个问题需要的东西"打成一个 zip。

为什么不自动上传：没有服务器，而且一按按钮就把群名/消息传走不是这个项目的做法。
用户自己看一眼、发给你，或者贴到 Issue。

里面有什么：
  · `反馈.txt`      —— 界面那份反馈原文（版本、配置概况、最近动作）
  · `事件日志.jsonl` —— 最近 2000 条结构化错误/异常（带 traceback）
  · `后台日志.txt`   —— background.log 最后 300 行
  · `配置-脱敏.toml` —— config.toml，**密钥/token 全打码**
  · `系统.txt`      —— 版本、提交、Python、内存、插件清单
  · `说明.txt`      —— 这份包里有什么、哪些东西故意没放

**默认不含消息原文和附件**；要包含得调用方显式说（目前没有入口）。
"""

from __future__ import annotations

import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

from .config import default_data_dir, default_root

_SECRET = re.compile(
    r"(?i)((?:access_token|api_key|key|token|password|webhook)\s*[=:]\s*)['\"]?[^\s'\"&]+")
_USER = re.compile(r"\\Users\\[^\\]+")


def _redact(text: str) -> str:
    return _USER.sub(r"\\Users\\…", _SECRET.sub(r"\1***", str(text or "")))


def _tail(path: Path, lines: int = 300) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(data[-lines:])


def _events(limit: int = 2000) -> str:
    """最近几天的结构化日志，拼成一段（新的在后）。"""
    rows: list[str] = []
    try:
        files = sorted((default_data_dir() / "logs").glob("events-*.jsonl"))
        for path in files:
            rows.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        pass
    return "\n".join(rows[-limit:])


def build(report_text: str, plugins: str = "", include_text: bool = False) -> Path:
    """打一个 zip，返回路径。`report_text` 是界面上那份反馈原文。"""
    root = default_root()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = default_data_dir() / f"诊断包-{stamp}.zip"
    config = root / "config.toml"
    config_text = _redact(config.read_text(encoding="utf-8", errors="replace")) \
        if config.is_file() else "（没有 config.toml）"
    readme = "\n".join([
        "私人秘书 · 诊断包",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "里面有什么：",
        "  反馈.txt        界面上那份反馈（版本、配置概况、最近动作）",
        "  事件日志.jsonl  结构化的错误/异常（带完整堆栈），一行一条",
        "  后台日志.txt    background.log 最后 300 行",
        "  配置-脱敏.toml  config.toml，密钥和 token 已打码",
        "  系统.txt        版本、Python、内存、插件清单",
        "",
        "故意没放：消息原文、附件、待办内容。" if not include_text
        else "注意：这份**包含消息原文**（是你自己勾的）。",
        "",
        "发出去之前自己扫一眼 —— 里面可能有群名。",
    ])
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("说明.txt", readme)
        zf.writestr("反馈.txt", _redact(report_text))
        zf.writestr("事件日志.jsonl", _redact(_events()))
        zf.writestr("后台日志.txt", _redact(_tail(default_data_dir() / "background.log")))
        zf.writestr("配置-脱敏.toml", config_text)
        info = root / "build-info.txt"
        zf.writestr("系统.txt", "\n".join([
            (info.read_text(encoding="utf-8", errors="replace").strip()
             if info.is_file() else "（没有 build-info.txt，源码运行）"),
            plugins or "（没有插件清单）",
        ]))
    return target
