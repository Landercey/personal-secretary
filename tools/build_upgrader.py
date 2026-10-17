"""把「补丁包 + 一键升级脚本」打成一个 exe，双击就能升。

用法（在项目根目录）：
    python tools/build_upgrader.py 补丁包路径 输出目录

产物名里带版本号：`私人秘书-一键升级-v0.3.21.exe`。
它比补丁包大（补丁包整个塞进去了），但用户只需要双击一次。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python tools/build_upgrader.py 补丁包.zip [输出目录]")
        return 1
    patch = Path(sys.argv[1]).resolve()
    if not patch.is_file():
        print(f"找不到补丁包：{patch}")
        return 1
    out_dir = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else ROOT / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)

    version = "?"
    with zipfile.ZipFile(patch) as zf:
        try:
            info = zf.read("build-info.txt").decode("utf-8", "replace")
            version = info.split("｜")[0].strip().lstrip("v") or "?"
        except KeyError:
            pass
    name = f"私人秘书-一键升级-v{version}"

    work = ROOT / "build" / "upgrader"
    if work.is_dir():
        shutil.rmtree(work)             # 只删我们自己刚建的中间目录
    work.mkdir(parents=True)
    # 载荷改个不会和别的东西重名的名字，PyInstaller 会原样放进 _MEIPASS
    payload = work / "payload.zip"
    shutil.copy2(patch, payload)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",                    # 单个 exe，用户不用管旁边有没有文件夹
        "--console",                    # 留黑框：进度和"关掉哪份程序"要说给用户听
        "--name", name,
        "--icon", str(ROOT / "assets" / "app.ico"),
        "--workpath", str(work),
        "--specpath", str(work),
        "--distpath", str(out_dir),
        "--add-data", f"{payload}{';' if sys.platform == 'win32' else ':'}.",
        str(ROOT / "tools" / "upgrader.py"),
    ]
    print("打包升级器…")
    code = subprocess.run(args, cwd=str(ROOT)).returncode
    target = out_dir / f"{name}.exe"
    if code != 0 or not target.is_file():
        print(f"打包失败（退出码 {code}）")
        return code or 1
    print(f"好了：{target}（{target.stat().st_size // 1048576} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
