"""一键升级：把补丁铺进已经装好的软件文件夹，然后重新打开。

为什么要有这个东西（用户的原话："单独给新用户搞个升级安装包，旧版本直接升"）：
补丁包是个 zip，让初学者"解压到软件文件夹里覆盖"很容易出事 —— 他多半会解压到
下载文件夹，于是多出一份**配置为空**的程序；再启动就变成两份抢同一个端口。
这个 exe 把"关程序 → 覆盖 → 重新打开"三步做完，用户只需要双击。

怎么找到旧那份（按顺序）：
  1. 命令行给路径（把软件文件夹拖到这个 exe 上也算这条）；
  2. 它自己就躺在软件文件夹里（最常见的用法：把它放进那堆文件里双击）；
  3. 桌面上那条「私人秘书」快捷方式指向哪儿。

放进 exe 的 payload.zip 只含程序文件（exe + `_internal`），
config.toml / data / plugins / napcat 一个都不在里面 —— 升级不碰用户的配置和待办。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

APP = "私人秘书.exe"
PAYLOAD = "payload.zip"

# 控制台是 GBK（cp936）：一个 ✅ 就能让升级脚本在最后一步崩掉（v0.3.17 踩过同款）。
# 从根上兜住：打不出来的字符换成 ?，升级流程绝不因为一句提示中断。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")


def say(text: str = "") -> None:
    print(text, flush=True)

# 出错时窗口不能一闪就没 —— 用户得看见"关掉另一份程序"那几行字。
def pause() -> None:
    try:
        input("\n按回车键关闭这个窗口…")
    except EOFError:
        time.sleep(8)


def payload_path() -> Path:
    """PyInstaller 单文件版会把 --add-data 的东西解到 _MEIPASS 里。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / PAYLOAD


def _shortcut_target() -> Path | None:
    """桌面那条快捷方式指向哪个 exe（我们第一次运行时自动建的那条）。"""
    lnk = Path(os.environ.get("USERPROFILE", "")) / "Desktop" / "私人秘书.lnk"
    if not lnk.is_file():
        return None
    script = (
        # 先让 PowerShell 用 UTF-8 输出：它的默认输出是系统代码页（GBK），
        # 而这边的 text=True 按 Python 自己的 locale 解码 —— 中文路径会变成
        # "绉佷汉绉樹功" 这种乱码，于是"快捷方式明明在，却说找不到"。
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('"
        + str(lnk) + "'); if($s.TargetPath){Write-Output $s.TargetPath}"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or "").strip()
    if text and Path(text).is_file():
        return Path(text).parent
    return None


def find_target() -> Path | None:
    # 支持三种给法：直接拖文件夹到这个 exe 上、`--dir 路径`、`--dir=路径`
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    for item in sys.argv[1:]:
        if item.startswith("--dir="):
            args.insert(0, item.split("=", 1)[1])
    if args:
        given = Path(args[0].strip('"'))
        if given.is_file():
            given = given.parent
        if (given / APP).is_file():
            return given
        say(f"那个文件夹里没有「{APP}」：{given}")
        return None
    here = Path(sys.executable).resolve().parent
    if (here / APP).is_file():
        return here
    return _shortcut_target()


def _is_temp(path: Path) -> bool:
    """是不是从压缩包里跑出来的（exe 在 %TEMP% 下面）。"""
    try:
        temp = Path(tempfile.gettempdir()).resolve()
        resolved = path.resolve()
    except OSError:
        return False
    return temp == resolved or temp in resolved.parents


def stop_app() -> None:
    """先关掉正在跑的那份 —— 不关的话 exe 被占着，覆盖会失败。"""
    subprocess.run(["taskkill", "/F", "/IM", APP],
                   capture_output=True, text=True)
    time.sleep(1.5)


def extract(payload: Path, target: Path) -> list[str]:
    """把补丁铺进去。返回失败的文件名（通常是被占着的 DLL）。"""
    failed: list[str] = []
    with zipfile.ZipFile(payload) as zf:
        names = [n for n in zf.namelist() if n and not n.endswith("/")]
        say(f"正在覆盖 {len(names)} 个文件…")
        for index, name in enumerate(names, 1):
            dest = target / name
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
            except OSError as exc:
                failed.append(f"{name}（{exc.strerror or exc}）")
            if index % 200 == 0:
                say(f"  {index}/{len(names)}")
    return failed


def main() -> int:
    say("=" * 56)
    say("  私人秘书 · 一键升级")
    say("=" * 56)

    payload = payload_path()
    if not payload.is_file():
        say(f"升级包坏了：找不到 {PAYLOAD}。重新下一份吧。")
        pause()
        return 1
    with zipfile.ZipFile(payload) as zf:
        built = ""
        try:
            built = zf.read("build-info.txt").decode("utf-8", "replace").strip()
        except KeyError:
            pass
    say(f"要升到：{built or '（升级包里没写版本）'}")

    target = find_target()
    if target is None:
        say()
        say("找不到旧版本装在哪。两种办法，随便一种：")
        say(f"  1. 把这个升级包 **放进软件文件夹**（和「{APP}」同一个地方）再双击；")
        say("  2. 把软件文件夹直接拖到这个 exe 上。")
        pause()
        return 1
    if _is_temp(target):
        say()
        say("注意：你这份是从压缩包里直接跑起来的（在临时目录里）。")
        say("   先把它解压成一个文件夹再用这个升级包，不然升完还是会丢配置。")
        pause()
        return 1

    say(f"旧版本位置：{target}")
    say("正在关掉旧版本…")
    stop_app()
    failed = extract(payload, target)
    if any(name.startswith(APP) for name in failed):
        say()
        say(f"注意：「{APP}」没能覆盖 —— 它还在被占用。")
        say("   打开任务管理器搜「私人秘书」，结束掉所有条目，再双击本升级包。")
        pause()
        return 1
    if failed:
        # 通常是两个 VCRUNTIME DLL：别的进程（QQ/NapCat）也加载了它们。
        # 内容一模一样，覆盖不了不影响使用。
        say(f"（{len(failed)} 个系统 DLL 被占用，内容相同，跳过）")

    say("升级完成。")
    if "--no-start" in sys.argv:
        return 0
    exe = target / APP
    say("正在重新打开…")
    # 先起后台（收消息），再开窗口 —— 和平时一样，用户不用再点「启动 QQ」。
    subprocess.Popen([str(exe), "--serve"], cwd=str(target),
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)
    time.sleep(1.5)
    subprocess.Popen([str(exe)], cwd=str(target))
    say("好了，窗口应该已经打开了。这个升级包可以删掉。")
    time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
