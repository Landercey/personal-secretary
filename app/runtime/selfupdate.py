"""一键更新：把新版补丁包下下来，覆盖自己，再把自己打开。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  改补丁包的下载地址规律   → `patch_url()`（**按版本号拼**，不查接口 —— 见下面的注释）
  改下载的大小上限/超时    → `MAX_BYTES` / `download()`
  改更新脚本干什么         → `updater_script()`（纯 ASCII 批处理，覆盖完自己删掉）
  改"更新完还起不起后台"   → `updater_script(restart_receiver=...)` + bridge.install_update
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
import urllib.request
from pathlib import Path

ZIP_NAME = "update.zip"
LOG_NAME = "update.log"
SCRIPT_NAME = "update.cmd"

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


def patch_url(repo: str, version: str) -> str:
    """按版本号拼出补丁包的下载地址（不请求任何接口）。"""
    clean = str(version or "").strip().lstrip("vV")
    return PATCH_URL.format(repo=repo, version=clean)


def download(url: str, dest: Path, timeout: float = 300.0) -> Path:
    """把包下到 dest。太大、下不动都抛异常（调用方负责告诉用户）。"""
    request = urllib.request.Request(url, headers={"User-Agent": "personal-secretary"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        declared = int(response.headers.get("Content-Length") or 0)
        if declared > MAX_BYTES:
            raise ValueError(f"文件太大（{declared // 1048576} MB），不像是个补丁包")
        blob = response.read(MAX_BYTES + 1)
    if len(blob) > MAX_BYTES:
        raise ValueError("下载的东西太大，不像是个补丁包")
    dest.write_bytes(blob)
    return dest


def updater_script(app_dir: Path, pid: int, restart_receiver: bool = False) -> Path:
    """写更新脚本，返回它的路径（放在程序自己的目录里）。

    `pid` 是**当前进程**（界面）的：脚本先等它消失再动手覆盖。
    后台那个进程（`--serve`）得在调这个之前就停掉 —— 它是同一个 exe，
    不停的话脚本会一直等下去。
    """
    script = app_dir / SCRIPT_NAME
    # 起来之后按顺序：先起后台（如果原来在跑），再开界面 ——
    # 界面一起来就会去读心跳，后台先起来它才不会显示"后台没在跑"。
    start_lines = ["for %%f in (*.exe) do start \"\" \"%%f\""]      # 界面
    if restart_receiver:
        start_lines.insert(0, "for %%f in (*.exe) do start \"\" \"%%f\" --serve")
    script.write_text("\r\n".join([
        "@echo off",
        "cd /d \"%~dp0\"",
        # 注释也必须是 ASCII —— 中文进了批处理就可能变乱码（单测盯着这一点）
        "rem wait until the old app exits: a running exe cannot be overwritten",
        ":wait",
        f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul',
        "if not errorlevel 1 (",
        "  timeout /t 1 /nobreak >nul",
        "  goto wait",
        ")",
        f'tar -xf "{ZIP_NAME}" -C "." > "{LOG_NAME}" 2>&1',
        "if errorlevel 1 goto failed",
        f'del "{ZIP_NAME}"',
        f"echo OK >> \"{LOG_NAME}\"",
        *start_lines,
        "del \"%~f0\"",
        "exit /b 0",
        ":failed",
        f'echo FAILED >> "{LOG_NAME}"',
        "del \"%~f0\"",
        "exit /b 1",
        "",
    ]), encoding="ascii", newline="")
    return script


def launch(script: Path) -> None:
    """把脚本启动起来，然后调用方就该退出了。

    用 DETACHED_PROCESS：它得**活得比我们久**（我们在等它把我们覆盖掉），
    顺便也就没有窗口了（这条路和 CREATE_NO_WINDOW 不能一起用 ——
    见 deploy.no_window_flags 的注释）。
    """
    subprocess.Popen(["cmd", "/c", str(script)], cwd=str(script.parent),
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)
