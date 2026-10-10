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
import os
import subprocess
import sys
from pathlib import Path

from .config import default_root

SHORTCUT_NAME = "私人秘书.lnk"
AUTOSTART_NAME = "私人秘书(开机自启).lnk"


def frozen_exe() -> Path | None:
    """打包成 exe 之后，程序自己那个 exe 在哪。源码运行时返回 None。

    为什么要区分：源码运行是「pythonw.exe run.py --ui」两个东西拼出来的命令，
    打成 exe 之后目标就是 exe 本身、**不带任何参数**（双击它就该开界面）。
    快捷方式指向哪儿不一样，是这整个文件里唯一需要分情况的地方。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return None


def launcher(root: Path) -> tuple[Path | None, str]:
    """(要启动的程序, 图标位置) —— 快捷方式和开机自启共用这一份判断。

    图标：exe 情况下用「exe 自己,0」（第 0 号图标资源就是打包时嵌进去的小肥鱼），
    不用旁边再放一个 .ico 文件；源码运行还是用 assets 里那个。
    参数由调用方拼：快捷方式是开界面，开机自启是 `--serve`。
    """
    exe = frozen_exe()
    if exe is not None:
        return exe, f"{exe},0"
    return pythonw(), str(icon_path(root))


# 在 PowerShell 里跑的一段脚本。用 {} 占位，由 Python 填好再编码。
_SCRIPT = """
$ErrorActionPreference = 'Stop'
# 不关掉进度流的话，输出里会混进一坨 CLIXML（powershell 首次加载模块时的进度记录）
$ProgressPreference = 'SilentlyContinue'
$root  = '{root}'
$iconloc = '{iconloc}'
$target = '{target}'
$args   = '{arguments}'
$link  = Join-Path ([Environment]::GetFolderPath('Desktop')) '{name}'

if ('{only_if_missing}' -eq '1' -and (Test-Path $link)) {{ Write-Output $link; exit 0 }}
if (-not (Test-Path $target)) {{ throw ('找不到要启动的程序：' + $target) }}
# 图标位置可以带序号（'...\程序.exe,0'）—— 判断"文件在不在"得先把序号去掉。
$iconfile = $iconloc -replace ',\d+$', ''

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath       = $target
$sc.Arguments        = $args
$sc.WorkingDirectory = $root
$sc.Description      = '私人秘书 · 打开待办界面'
if ($iconloc -and (Test-Path $iconfile)) {{ $sc.IconLocation = $iconloc }}
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


def create_shortcut(root: Path | None = None,
                    only_if_missing: bool = False) -> tuple[bool, str]:
    """在桌面建「私人秘书」快捷方式。返回 (成功?, 说明)。

    注意：快捷方式**只打开界面**。后台收消息要另外跑 `run.py --qq`，
    因为界面关掉不该把收消息一起关掉。

    `only_if_missing=True` 用在"打包版第一次打开"这条路上：桌面上已经有了
    就一根手指都不碰（那可能是他自己调过图标位置的），没有才建。
    """
    project = Path(root) if root else default_root()
    target, icon = launcher(project)
    if target is None:
        return False, "找不到 pythonw.exe（装 Python 时没勾上？）"
    # exe：双击它就是开界面，所以不带参数；源码：得写清楚跑哪个 run.py
    arguments = "" if frozen_exe() else f'"{project / "run.py"}" --ui'
    script = _SCRIPT.format(root=str(project).replace("'", "''"),
                            iconloc=str(icon).replace("'", "''"),
                            target=str(target).replace("'", "''"),
                            arguments=arguments.replace("'", "''"),
                            only_if_missing="1" if only_if_missing else "0",
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


# --------------------------------------------------------------------- 开机自启
def startup_folder() -> Path:
    """Windows 的「启动」文件夹。放进去的快捷方式开机就会自己跑。"""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return (Path(base) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup")


def autostart_entry() -> Path:
    return startup_folder() / AUTOSTART_NAME


def autostart_enabled() -> bool:
    return autostart_entry().is_file()


def set_autostart(enabled: bool, root: Path | None = None) -> tuple[bool, str]:
    """开/关开机自启。返回 (成功?, 说明)。

    **挂的是哪一个进程**：`run.py --serve` —— 一个进程同时干收消息和定时推送。
    以前这俩是分开的，开机自启就得挂两条、多一个崩溃点，所以合成一条了。

    关掉就是删掉那个快捷方式。挪了文件夹之后要重新开一次
    （快捷方式里存的是绝对路径）。
    """
    entry = autostart_entry()
    if not enabled:
        try:
            entry.unlink()
        except FileNotFoundError:
            return True, "本来就是关着的"
        except OSError as exc:
            return False, f"删不掉：{exc}"
        return True, "已关掉开机自启"

    project = Path(root) if root else default_root()
    target, icon = launcher(project)
    if target is None:
        return False, "找不到 pythonw.exe（装 Python 时没勾上？）"
    # exe：`私人秘书.exe --serve`；源码：`pythonw.exe run.py --serve`
    arguments = ("--serve" if frozen_exe()
                 else f'"{project / "run.py"}" --serve')
    try:
        entry.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"建不了启动文件夹：{exc}"

    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        "$shell = New-Object -ComObject WScript.Shell\n"
        f"$link = $shell.CreateShortcut('{str(entry).replace(chr(39), chr(39) * 2)}')\n"
        f"$link.TargetPath = '{str(target).replace(chr(39), chr(39) * 2)}'\n"
        f"$link.Arguments = '{arguments.replace(chr(39), chr(39) * 2)}'\n"
        f"$link.WorkingDirectory = '{str(project).replace(chr(39), chr(39) * 2)}'\n"
        f"$link.IconLocation = '{str(icon).replace(chr(39), chr(39) * 2)}'\n"
        "$link.Description = '私人秘书 · 后台收消息 + 定时推送'\n"
        "$link.Save()\n"
    )
    ok, output = _run_powershell(script)
    if not ok:
        return False, output or "PowerShell 建快捷方式失败"
    return True, "已开启：下次开机它会自己跑起来"
