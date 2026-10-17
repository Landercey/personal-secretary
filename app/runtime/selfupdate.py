"""一键更新：把新版补丁包下下来，覆盖自己，再把自己打开。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  改补丁包的下载地址规律   → `patch_url()`（**按版本号拼**，不查接口 —— 见下面的注释）
  改下载的大小上限/超时    → `MAX_BYTES` / `download()`
  改"谁来覆盖文件"         → `run_upgrader()`：跑那个一键升级包（它自己关旧版、覆盖、重开）
  改失败怎么办             → 脚本把日志写进 `update.log`，程序下次启动不静默

=====================================================================
为什么可以这么简单
=====================================================================
补丁包里只有 `私人秘书.exe` 和 `_internal/`（见 tools/build_exe.py 的
make_patch_zip）。配置、待办、插件、下好的 QQ 全在**别的文件夹**里 ——
所以覆盖这两样不会碰到用户的任何东西，也**不影响 QQ 的登录状态**
（登录态在 napcat/ 里），更新完不用重新扫码。

=====================================================================
为什么必须借一个小脚本来做
=====================================================================
Windows 上**正在运行的 exe 覆盖不了**，所以只能：

  1. 下好包 → 2. 写一个小脚本 → 3. 启动它 → 4. 自己退出 →
  5. 脚本等我们退干净 → 覆盖 → 6. 再把程序打开

脚本特意写成**纯 ASCII**：用 `%~dp0` 当工作目录（不写死路径）、
用 **PID** 等我们退出（不写中文进程名）、用 `*.exe` 通配符找程序
（不写中文文件名）。批处理里的中文在 cmd 的代码页下很容易变乱码 ——
这个项目已经为编码踩过好几次了。

解压用 **Windows 自带的 tar**（bsdtar，Win10 1803 起就有，能直接开 zip，
默认覆盖）——不引第三方依赖，也不额外下东西。
"""

from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.request
from http.client import HTTPException
from pathlib import Path

# 补丁包约 38 MB。给足余量的同时挡一下"下错东西"（下到 2 GB 的东西就别下了）。
MAX_BYTES = 200 * 1024 * 1024


# GitHub 的 Release 下载地址是有规律的：知道版本号就能拼出来。
#
# 为什么**不用 API 拿附件地址**（2026-10-13 真踩过）：GitHub 的接口对未登录请求
# **每小时只给 60 次**。作者点「检查更新」时正好撞上限流（HTTP 403），
# 界面却报"连不上网"，让人以为是自己断网 —— 其实是我发版那天查得太频。
# 版本号走 raw 文件那条路（不受限流），地址按规律拼，整条链路就不碰 API 了。
PATCH_URL = ("https://github.com/{repo}/releases/download/v{version}/"
             "personal-secretary-v{version}-patch.zip")
# 一键升级包：45 MB，双击就会"关旧版 → 覆盖 → 重开"。
# 为什么不用以前那套"写个批处理、等 PID 退出再 tar"（2026-10-17 实测废掉）：
# detached 起 cmd 时，`tasklist | find` 那个管道会**卡死**（find 一直等输入），
# 结果就是：程序被关掉了、文件没换成、什么也没重开。升级包这条路实测能过。
UPGRADE_URL = ("https://github.com/{repo}/releases/download/v{version}/"
               "personal-secretary-v{version}-upgrade.exe")


def patch_url(repo: str, version: str) -> str:
    """按版本号拼出补丁包的下载地址（不请求任何接口）。"""
    clean = str(version or "").strip().lstrip("vV")
    return PATCH_URL.format(repo=repo, version=clean)


def upgrade_url(repo: str, version: str) -> str:
    """一键升级包的下载地址（同样是按版本号拼，不查接口）。"""
    clean = str(version or "").strip().lstrip("vV")
    return UPGRADE_URL.format(repo=repo, version=clean)


def download(url: str, dest: Path, timeout: float = 300.0,
             tries: int = 3) -> Path:
    """把包下到 dest。太大、下不动都抛异常（调用方负责告诉用户）。

    断一次要重试：37 MB 走代理传到一半断掉是常态（我自己发版时撞过好几次 EOF）。
    让用户点一下就得重头再来，体验比"慢 3 秒"差得多。
    只有"太大"这种明确的错不重试 —— 那说明地址不对，重试也是白搭。
    """
    last: Exception | None = None
    blob = b""
    for attempt in range(1, max(1, tries) + 1):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "personal-secretary"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                declared = int(response.headers.get("Content-Length") or 0)
                if declared > MAX_BYTES:
                    raise ValueError(
                        f"文件太大（{declared // 1048576} MB），不像是个补丁包")
                blob = response.read(MAX_BYTES + 1)
            break
        except ValueError:
            raise
        except (urllib.error.URLError, HTTPException, OSError) as exc:
            last = exc
            if attempt >= tries:
                raise
            time.sleep(2 * attempt)
    if len(blob) > MAX_BYTES:
        raise ValueError("下载的东西太大，不像是个补丁包")
    dest.write_bytes(blob)
    return dest


def run_upgrader(exe: Path, app_dir: Path) -> None:
    """把一键升级包启动起来；调用方随后就该退出（它会关掉我们）。

    为什么交给它做：覆盖程序文件这事要在"旧进程已经退出"之后 ——
    自己等 PID、自己写批处理那条路在真实环境里卡死过（见 UPGRADE_URL 上面的注释）。
    升级包这边是**在控制台窗口里跑**的（不 detached）：进度、失败原因用户都看得见，
    失败时它还会停在那儿等回车，不会一闪就没。
    """
    # console-ok：这个黑框**就是**升级的进度界面，故意留着的
    subprocess.Popen([str(exe), "--dir", str(app_dir)], cwd=str(exe.parent))
