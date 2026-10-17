"""发 Release：把这一版的三个包传上去（缺哪个补哪个）。

用法（在项目根目录，先跑过 tools/build_exe.py）：
    python tools/github_release.py 0.3.21 [--dist 打包输出目录]

三个包：
  · 补丁包（覆盖用）      → personal-secretary-vX.Y.Z-patch.zip
  · 完整版（新用户用）    → personal-secretary-vX.Y.Z-full.zip
  · 一键升级（老用户用）  → personal-secretary-vX.Y.Z-upgrade.exe

（GitHub 会把资源名里的中文换成 `-`，所以本地叫「私人秘书-一键升级-vX.exe」，
传上去用英文名；用户下载后自己改回中文名也行，不影响。）

为什么要"缺哪个补哪个"：这台机器上 GitHub 的 37 MB 上传经常被代理掐断（EOF），
传一半失败是常态。重跑一次不该把已经传好的再传一遍，也不该因为
"这个 tag 已经有 Release 了"直接退出。

凭证：走 git 的 credential helper（`gho_` 开头的 OAuth token），只在进程里用，不打印。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "Landercey/personal-secretary"
API = "https://api.github.com"
GIT = (r"C:\Users\15118\.cache\codex-runtimes\codex-primary-runtime"
       r"\dependencies\native\git\cmd\git.exe")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def token() -> str:
    out = subprocess.run([GIT, "credential", "fill"],
                         input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, timeout=30).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("拿不到凭证（git credential fill 没吐 password）")


TOKEN = token()


def call(method: str, url: str, data=None, content_type="application/json",
         tries: int = 5):
    body = None if data is None else (
        json.dumps(data).encode() if content_type == "application/json" else data)
    last: Exception | None = None
    for attempt in range(1, tries + 1):
        request = urllib.request.Request(url, data=body, method=method, headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "personal-secretary-release",
            "Content-Type": content_type,
        })
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                text = response.read().decode("utf-8", "replace")
                return json.loads(text) if text.strip().startswith(("{", "[")) else text
        except urllib.error.HTTPError:
            # 4xx/5xx 是**确定的答案**（比如"这个 tag 还没有 Release"是 404），
            # 重试只会把 404 拖成 5 次 404，外面那个 except 永远等不到它。
            raise
        except (urllib.error.URLError, OSError) as exc:   # 代理断流是常态
            last = exc
            print(f"    第 {attempt} 次失败：{type(exc).__name__}: {exc}")
            time.sleep(3 * attempt)
    raise SystemExit(f"重试 {tries} 次还是不行：{last}")


def find_assets(dist: Path, version: str) -> list[tuple[Path, str]]:
    """本地找到这一版的三个包，返回 [(路径, 传上去叫什么)]。"""
    found: list[tuple[Path, str]] = []
    for pattern, remote in (
            (f"补丁包-v{version}-*.zip", f"personal-secretary-v{version}-patch.zip"),
            (f"完整版-v{version}.zip", f"personal-secretary-v{version}-full.zip"),
            (f"私人秘书-一键升级-v{version}*.exe",
             f"personal-secretary-v{version}-upgrade.exe")):
        matches = sorted(dist.glob(pattern))
        if matches:
            found.append((matches[-1], remote))
        else:
            print(f"  ! 本地没找到：{pattern}")
    return found


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python tools/github_release.py 0.3.21 [--dist 目录]")
        return 1
    version = sys.argv[1].lstrip("v")
    tag = f"v{version}"
    dist = Path(sys.argv[sys.argv.index("--dist") + 1]) if "--dist" in sys.argv \
        else ROOT / "dist"

    note = ROOT / f"release-note-{version}.md"
    assets = find_assets(dist, version)
    if not assets:
        print(f"{dist} 里一个包都没找到，先跑 tools/build_exe.py")
        return 1

    try:
        release = call("GET", f"{API}/repos/{REPO}/releases/tags/{tag}")
        print("Release 已经在了：", release["html_url"])
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        release = call("POST", f"{API}/repos/{REPO}/releases", {
            "tag_name": tag,
            "target_commitish": "main",
            "name": tag,
            "body": note.read_text(encoding="utf-8") if note.is_file() else "",
            "draft": False,
            "prerelease": False,
        })
        print("Release 建好了：", release["html_url"])

    have = {a["name"] for a in release.get("assets", [])}
    for path, remote in assets:
        if remote in have:
            print(f"  跳过（已传过）{remote}")
            continue
        blob = path.read_bytes()
        call("POST",
             f"https://uploads.github.com/repos/{REPO}/releases/"
             f"{release['id']}/assets?name={urllib.parse.quote(remote)}",
             blob, content_type="application/octet-stream")
        print(f"  传上去了 {remote}（{len(blob) // 1048576} MB）")

    final = call("GET", f"{API}/repos/{REPO}/releases/tags/{tag}")
    print("\n最终核对：")
    for asset in final.get("assets", []):
        print(f"  {asset['name']}  {asset['size'] // 1048576} MB  {asset['state']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
