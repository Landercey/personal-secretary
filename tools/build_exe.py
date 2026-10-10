"""把项目打包成**免安装的绿色文件夹**（PyInstaller onedir）。

怎么用：

    python tools/build_exe.py

产出（dist/ 不进仓库，见 .gitignore）：

    dist/私人秘书/私人秘书.exe      ← 双击这个，图标就是那条小肥鱼
    dist/私人秘书/_internal/        ← 程序自己的文件，用户不用管也别删

第一次双击 exe 之后，它旁边会长出用户自己的东西（都在同一个文件夹里，
卸载 = 删掉整个文件夹）：

    config.toml     配置
    data/           待办、附件、日志、你登录的 QQ 数据
    plugins/        插件（自带的课表插件这时才拷进来）
    napcat/         要收 QQ 消息时自动装的 NapCat

=====================================================================
为什么是 onedir，不是单文件 exe
=====================================================================
· 单文件 exe 每次启动都要把自己解压到临时目录 —— 启动慢一倍不止（实测）；
· 单文件 exe 是"自解压"行为，杀软误报率明显更高。
绿色文件夹"复制走就能用"，代价只是多一层 `_internal`。

⚠️ **重新打包会删掉 `dist/私人秘书/` 整个文件夹**（PyInstaller 的行为）。
里面有你自己的待办和附件 —— 要重新打包之前，先把 `dist/私人秘书` 复制到
别处，或者只打包、别用它当日常在用的那一份。

=====================================================================
打包时容易踩的三个坑（都在下面处理了）
=====================================================================
1. **界面 html / schema.sql / OCR 脚本**不是 .py，PyInstaller 不会自己带 ——
   必须用 --add-data 显式塞进去，而且要保持原来的相对路径：
   `Path(__file__).with_name("schema.sql")` 在打包后指向 `_internal` 里的同名位置。
2. **pywebview 的平台模块是运行时才 import 的**，静态分析看不见 ——
   得用 --hidden-import 点名（Windows = edgechromium + winforms）。
3. **zhconv 的字典是一个 json 数据文件**，只收集 .py 会把繁简归一弄丢 ——
   用 --collect-data 带上，收集不到的话繁体消息的时间会全部认错。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "私人秘书"
DIST = ROOT / "dist" / APP_NAME
EXE = DIST / f"{APP_NAME}.exe"

# 不是 .py、但程序运行时要读的文件。左边是项目里的路径，右边是打包后 `_internal` 里的位置。
DATA_FILES = [
    ("app/ui", "app/ui"),                                    # 界面（一个 html 全包）
    ("app/store/schema.sql", "app/store"),                    # 建表语句
    ("app/pipeline/extract/win_ocr.ps1", "app/pipeline/extract"),   # 系统 OCR 脚本
    ("plugins", "plugins"),                                   # 自带插件，首次运行拷到 exe 旁边
    ("assets", "assets"),                                     # 图标（窗口、快捷方式）
]

# 运行时才 import、静态分析看不见的模块。
HIDDEN_IMPORTS = [
    "webview.platforms.edgechromium",     # Windows 上真正干活的渲染器（WebView2）
    "webview.platforms.winforms",         # 窗口本身
    "webview.platforms.mshtml",           # 老系统兜底
    "clr",                                # pythonnet：上面两个都靠它
    "clr_loader",
]

# **绝对不许被拖进包里的东西。**
#
# 这些是本机装的机器学习/科学计算库（torch、transformers、librosa、faiss…），
# 这套电脑上有，装包时只要有一个看得见的 import 提到它们，PyInstaller 就会
# 顺着 hooks 把整个包全塞进来 —— 实测把程序从 90 MB 撑到 **5.2 GB**，
# 启动还慢。本程序一行都不用它们：
#   · RapidOCR（onnxruntime 那一路）是"用户自己 pip 装了才用"的可选件，
#     源码里已经改成 importlib 动态导入（见 extract/ocr_rapid.py）；
#   · 下面这些一个都不在本项目的依赖里（requirements.txt）。
# 留着这份名单是**保险**：以后谁手滑写了一个看得见的 import，也不会把
# 5 GB 的东西悄悄塞进发布包（那样只会表现为"安装包莫名其妙很大"）。
EXCLUDES = [
    "torch", "torchvision", "torchaudio", "functorch", "torchgen",
    "transformers", "librosa", "sklearn", "scipy", "matplotlib", "pandas",
    "numba", "llvmlite", "faiss", "faiss_cpu", "av", "onnxruntime",
    "rapidocr_onnxruntime", "cv2",
]


def check_inputs() -> None:
    icon = ROOT / "assets" / "app.ico"
    if not icon.is_file():
        raise SystemExit(f"找不到图标：{icon}\n（图标就是打包进 exe 的那条小肥鱼）")
    missing = [src for src, _ in DATA_FILES if not (ROOT / src).exists()]
    if missing:
        raise SystemExit("这些要一起打包的文件不见了：" + "、".join(missing))


def build() -> None:
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir",
        # 不带控制台窗口：双击 exe 不该先弹一个黑框。
        # 命令行的输出（--selftest 之类）在重定向到文件时照样能拿到。
        "--windowed",
        "--name", APP_NAME,
        "--icon", str(ROOT / "assets" / "app.ico"),
        # 中间产物放到 build/ 里，别把项目根目录弄乱
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        "--collect-data", "zhconv",
    ]
    for src, dest in DATA_FILES:
        args += ["--add-data", f"{ROOT / src}{os.pathsep}{dest}"]
    for name in HIDDEN_IMPORTS:
        args += ["--hidden-import", name]
    for name in EXCLUDES:
        args += ["--exclude-module", name]
    args.append(str(ROOT / "run.py"))

    print("打包中（第一次要几分钟）…")
    proc = subprocess.run(args, cwd=str(ROOT))
    if proc.returncode != 0:
        raise SystemExit(f"打包失败（PyInstaller 退出码 {proc.returncode}）")


def verify() -> None:
    """打完之后自己验一遍。**不许只因为"命令跑完了"就说成功。**"""
    if not EXE.is_file():
        raise SystemExit(f"没生成 exe：{EXE}")

    # ⓪ 体积闸门。程序本体应该在 100 MB 上下；一旦有几百 MB，
    # 说明 EXCLUDES 那份名单里有东西又被拖进来了（表现是"安装包莫名其妙很大"）。
    size_mb = sum(p.stat().st_size for p in DIST.rglob("*") if p.is_file()) / 1024 / 1024
    if size_mb > 400:
        raise SystemExit(f"打包出来 {size_mb:.0f} MB —— 有东西被拖进来了，"
                         "去检查 tools/build_exe.py 的 EXCLUDES")
    print(f"体积：{size_mb:.0f} MB")

    # ① 繁简归一的字典在不在（不在的话繁体消息的时间会全认错，而且不报错）
    dicts = list(DIST.rglob("zhcdict.json"))
    if not dicts:
        raise SystemExit("zhconv 的字典没打进去（繁体消息的时间会认错）")

    # ② 界面文件在不在
    if not list(DIST.rglob("index.html")):
        raise SystemExit("界面文件 index.html 没打进去")

    # ③ 真跑一次自检：能起来、配置能读、插件加载不炸
    log = ROOT / "build" / "selftest.txt"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    with open(log, "w", encoding="utf-8", errors="replace") as sink:
        code = subprocess.run([str(EXE), "--selftest"], cwd=str(ROOT),
                              stdout=sink, stderr=subprocess.STDOUT, env=env,
                              timeout=120).returncode
    text = log.read_text(encoding="utf-8", errors="replace")
    print(text.strip())
    if code != 0 or "契约完整" not in text:
        raise SystemExit(f"exe 自检没过（退出码 {code}），上面是它的输出")
    # 自检的输出里必须点名 exe 自己旁边那个文件夹 —— 不然说明路径判断错了，
    # 数据会写到 _internal 里去（用户永远找不到自己的待办）。
    if str(DIST) not in text:
        raise SystemExit("exe 把数据目录算到别处去了，检查 default_root()")


def make_shortcut() -> None:
    """桌面上建好快捷方式 —— 这就是"安装后自动生成"那一步。

    注意是**让 exe 自己去建**（`--shortcut`），不是这边代劳：
    这样验证的正好是用户点那个按钮时走的同一条路。
    """
    proc = subprocess.run([str(EXE), "--shortcut"], cwd=str(DIST),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120,
                          env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    print(((proc.stdout or "") + (proc.stderr or "")).strip())
    if proc.returncode != 0:
        raise SystemExit("快捷方式没建起来，看上面的报错")


README_TXT = """私人秘书 —— 用前必看

1. 双击「私人秘书.exe」就能用（不用装 Python，不用装任何东西）。
   桌面上那个「私人秘书」快捷方式也是同一个程序。

2. 整个「私人秘书」文件夹就是全部。想搬到别的电脑，整个文件夹复制过去就行。
   卸 载 = 关掉开机自启，然后把这个文件夹删掉。不留别的东西。

3. 第一次双击之后，这个文件夹里会多出你自己的东西：
      config.toml   你的配置（推送时间、企业微信地址、QQ 号…）
      data\\         你的待办、附件、日志
      plugins\\      插件（自带的课表插件在这里，删掉就是卸载它）
      napcat\\       要收 QQ 消息时自动下载的 QQ

4. 「_internal」这个文件夹是程序自己的零件，别删、别动 —— 删了程序就打不开。

5. 第一次打开，界面中间会有一张「还没弄完这几步」的清单，
   点哪一条就跳到对应的设置，跟着点完就能在手机上收到推送了。

6. 收 QQ 消息要么用小号、要么用大号：小号封了不心疼，大号群都加好了但风险
   落在主号上。作者自己用的是大号，自从用到现在没出过事 —— 这是经验，
   不是保证。本工具只收不发，不会主动发任何 QQ 消息。
"""


def write_readme() -> None:
    """在 exe 边上放一张"用前必看"。用普通文本，双击就能看，不挑软件。"""
    target = DIST / "用前必看.txt"
    target.write_text(README_TXT, encoding="utf-8-sig")
    print(f"已放好：{target.name}")


def write_build_info() -> None:
    """在 exe 边上写一行"这是哪一版"（时间 + 提交号）。

    为什么要这个东西：用户报问题时会附带一份"意见反馈"，第一行就是构建日期。
    以前打包版读不到 run.py，那里只会显示一个 `?` —— 我就得靠猜他装的是哪一版
    （真发生过：只能从他的日志里反推是哪一版）。有了这行就是一眼的事。
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace").stdout.strip()
        # 工作区脏的话标一下：不标的话这一版的提交号是**上一个**提交的，
        # 以后照着它去查代码会看错文件。
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace").stdout.strip()
        if dirty and commit:
            commit += "+未提交改动"
    except (OSError, subprocess.SubprocessError):
        commit = ""
    text = f"{stamp}｜提交 {commit or '未知'}"
    (DIST / "build-info.txt").write_text(text, encoding="utf-8")
    print(f"已记下这一版：{text}")


def main() -> int:
    check_inputs()
    build()
    verify()
    make_shortcut()
    write_readme()
    write_build_info()
    print()
    print("打包完成。软件就在这儿：")
    print(f"    {DIST}")
    print(f"（双击 {APP_NAME}.exe 就能用，桌面快捷方式已经建好了）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
