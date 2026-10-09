"""桌面快捷方式。

=====================================================================
为什么不用 .ps1 脚本
=====================================================================
第一版写了个 tools/make_shortcut.ps1，里面带中文 —— **直接崩了**。
原因：PowerShell 5.1 把 .ps1 当 ANSI（这台机器上是 GBK）读，
UTF-8 的中文变成乱码，整个脚本解析失败。

这个坑本项目里早就记过：`core/extract/win_ocr.ps1` 顶上就写着
"keep this file ASCII-only, PowerShell 5.1 reads it as ANSI"。

所以改成：脚本内容从 Python 传进去，用 PowerShell 的 `-EncodedCommand`
（命令行参数走 UTF-16，不受 .ps1 的编码规则影响）。旧项目的
NapCat 启动器也是这么干的，一样的原因。

=====================================================================
什么时候要重跑
=====================================================================
把项目文件夹挪了位置之后 —— 快捷方式里存的是绝对路径。
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

SHORTCUT_NAME = "私人秘书.lnk"

# 在 PowerShell 里跑的一段脚本。用 {} 占位，由 Python 填好再编码。
_SCRIPT = """
$ErrorActionPreference = 'Stop'
# 不关掉进度流的话，输出里会混进一坨 CLIXML（powershell 首次加载模块时的进度记录）
$ProgressPreference = 'SilentlyContinue'
$root  = '{root}'
$icon  = '{icon}'
$run   = Join-Path $root 'run.py'
$pyw   = '{pythonw}'
$link  = Join-Path ([Environment]::GetFolderPath('Desktop')) '{name}'

if (-not (Test-Path $pyw)) {{ throw 'pythonw.exe 找不到' }}
if (-not (Test-Path $run)) {{ throw 'run.py 找不到' }}

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath       = $pyw
$sc.Arguments        = '"' + $run + '" --ui'
$sc.WorkingDirectory = $root
$sc.Description      = '私人秘书 · 打开待办界面'
if (Test-Path $icon) {{ $sc.IconLocation = $icon }}
$sc.Save()
Write-Output $link
"""


def pythonw() -> Path | None:
    """找 pythonw.exe —— 用它启动才不弹黑窗口。"""
    import sys

    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.is_file() else None


def _run_powershell(script: str) -> tuple[bool, str]:
    """跑一段 PowerShell。用 -EncodedCommand 绕开 .ps1 的编码坑。"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            capture_output=True, timeout=60, encoding="utf-8", errors="replace",
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, output


def icon_path(project: Path) -> Path:
    """快捷方式该用哪个图标文件。

    Windows 按**文件路径**缓存图标，改了 app.ico 的内容但路径没变，
    桌面会继续显示老图标（ie4uinit 刷不动，得重启资源管理器）。
    tools/make_icon.py 因此额外存一份 app-<内容指纹>.ico 并写进
    assets/icon.json —— 内容一变文件名就变，缓存必然失效。
    这里优先读它，读不到就退回 app.ico。
    """
    assets = project / "assets"
    meta = assets / "icon.json"
    if meta.is_file():
        try:
            name = json.loads(meta.read_text(encoding="utf-8")).get("shortcut_icon")
            if name and (assets / name).is_file():
                return assets / name
        except (OSError, ValueError):
            pass
    return assets / "app.ico"


def create_shortcut(root: Path | None = None) -> tuple[bool, str]:
    """在桌面建「私人秘书」快捷方式。返回 (成功?, 说明)。

    注意：快捷方式**只打开界面**。后台收消息要另外跑 `run.py --qq`，
    因为界面关掉不该把收消息一起关掉。
    """
    project = Path(root) if root else Path(__file__).resolve().parents[2]
    interpreter = pythonw()
    if interpreter is None:
        return False, "找不到 pythonw.exe（装 Python 时没勾上？）"
    script = _SCRIPT.format(root=str(project).replace("'", "''"),
                            icon=str(icon_path(project)).replace("'", "''"),
                            pythonw=str(interpreter).replace("'", "''"),
                            name=SHORTCUT_NAME)
    ok, output = _run_powershell(script)
    return ok, output


# 从 PowerShell 的输出里把目标路径捞出来的标记。
# 为什么要标记：PowerShell 有时会在输出前后混进 CLIXML（首次加载模块的进度记录），
# 直接取"最后一行"会被那些噪声顶掉。
_TARGET_MARK = "PSTARGET:"


def resolve_shortcut(path: Path) -> Path | None:
    """把 `.lnk` 快捷方式解析成它真正指向的文件。解析不出来返回 None。

    **为什么需要**：用户从桌面上拖进来的常常是快捷方式（1 KB 的二进制指路牌），
    它自己当然一个字的正文都没有。Windows 双击时会跟过去，
    我们也得跟过去 —— 不然用户只会想"我拖的就是那个 PPT，怎么读不出来"。

    **为什么用 PowerShell 而不是自己解 .lnk 的二进制**：快捷方式有十几种
    （MSI 广告式、UNC 路径、相对路径、控制面板项……），自己写总有一种解不出来；
    而 WScript.Shell 就是 Windows 自己的实现，它解不出来的通常也没救了。
    """
    quoted = str(path).replace("'", "''")
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        "$shell = New-Object -ComObject WScript.Shell\n"
        f"$link = $shell.CreateShortcut('{quoted}')\n"
        f"Write-Output ('{_TARGET_MARK}' + $link.TargetPath)\n"
    )
    ok, output = _run_powershell(script)
    if not ok:
        return None
    for line in output.splitlines():
        if line.startswith(_TARGET_MARK):
            value = line[len(_TARGET_MARK):].strip()
            return Path(value) if value else None
    return None
