"""有没有新版本。

=====================================================================
怎么判断"新版"（不用先发 Release 也能用）
=====================================================================
两条路，从简单到正式：

1. **读仓库默认分支里的 `app/version.py`** —— 只要你把新版推到 main 上，
   老用户就能看到"有新版本"。缺点是"推了代码但还没打包"也会提示。
2. **读 Releases API 的最新 tag** —— 只有你正式发了 Release 才提示，
   更准确。所以优先走这条，没有 Release 时才退回第 1 条。

两条都失败（没网、GitHub 抽风、仓库私有）就**当没有新版本**，绝不打扰用户。

=====================================================================
三条底线
=====================================================================
1. **不许卡住界面**：检查跑在后台线程里，界面只读缓存文件；
2. **不许天天问**：默认 24 小时最多一次（`[ui] update_hours`）；
3. **不许上传任何东西**：这是一个 GET，除了"有人在用这个软件"之外不泄露别的。
   关掉：`[ui] update_check = false`。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

from ..version import VERSION

CACHE_NAME = "update.json"
_RAW = "https://raw.githubusercontent.com/{repo}/main/app/version.py"
_RELEASES = "https://api.github.com/repos/{repo}/releases/latest"


def parse_version(text: str) -> tuple[int, ...]:
    """`"v0.2.1"` → `(0, 2, 1)`。认不出来的当 0，绝不抛。"""
    found = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(text or ""))
    if not found:
        return (0,)
    return tuple(int(part) for part in found.groups(default="0"))


def is_newer(remote: str, local: str = VERSION) -> bool:
    """远端版本比本地新吗。"""
    return parse_version(remote) > parse_version(local)


def _get(url: str, timeout: float) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": "personal-secretary", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(200_000).decode("utf-8", "replace")


def fetch_latest(repo: str, timeout: float = 6.0) -> str:
    """问 GitHub 最新的版本号。问不到返回空字符串（调用方当"没有新版"）。"""
    try:
        body = _get(_RELEASES.format(repo=repo), timeout)
        tag = str(json.loads(body).get("tag_name") or "").strip()
        if tag:
            return tag.lstrip("vV")
    except Exception:
        pass
    try:                                  # 没发 Release：读仓库里的版本号
        body = _get(_RAW.format(repo=repo), timeout)
        found = re.search(r'VERSION\s*=\s*"([^"]+)"', body)
        return found.group(1) if found else ""
    except Exception:
        return ""


def fetch_release(repo: str, timeout: float = 8.0) -> dict:
    """拿最新那个 Release 的完整信息（**附件地址在里面**）。问不到返回空字典。

    `fetch_latest` 只要一个版本号（左下角那行用它）；这个要的是附件 ——
    一键更新得知道补丁包放在哪个地址。
    """
    try:
        data = json.loads(_get(_RELEASES.format(repo=repo), timeout))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def cache_path() -> Path:
    from .config import default_data_dir

    return default_data_dir() / CACHE_NAME


def read_cache() -> dict:
    """读上次问到的结果（界面只读它，所以永远不会卡）。"""
    try:
        return json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_cache(latest: str, current: str = VERSION) -> None:
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"checked_at": time.time(), "latest": latest, "current": current},
            ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def need_check(hours: float) -> bool:
    """该不该再去问一次。hours<=0 = 每次都问。"""
    if hours <= 0:
        return True
    checked = float(read_cache().get("checked_at") or 0)
    return (time.time() - checked) > hours * 3600


def check_now(repo: str, timeout: float = 6.0) -> str:
    """问一次并写缓存。返回远端版本号（问不到就是空）。"""
    latest = fetch_latest(repo, timeout=timeout)
    write_cache(latest or VERSION)        # 问不到也记一笔，免得每次都重试
    return latest


def summary() -> dict:
    """给界面用：当前版本、远端版本、有没有新版、去哪儿下载。"""
    from .config import config

    cached = read_cache()
    latest = str(cached.get("latest") or "")
    repo = str(config.get("ui", "update_source", "") or "").strip()
    return {
        "current": VERSION,
        "latest": latest,
        "has_new": bool(latest) and is_newer(latest),
        "url": f"https://github.com/{repo}/releases" if repo else "",
        "enabled": bool(config.get("ui", "update_check", True)),
    }
