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

⚠️ **重新打包会删掉 `dist/私人秘书/` 整个文件夹**（PyInstaller 的行为），
里面有你自己的待办、附件、填好的配置、下好的 NapCat（约 1.5 GB）——
所以打完包会**自动把它们挪回来**（见 `stash_user_data`）。

⚠️ 另外：打包时**不能开着这个程序**（它锁着 `data/secretary.db`，
Windows 不让删，PyInstaller 会报"另一个程序正在使用此文件"而失败）。

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
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Windows 控制台默认是 GBK：脚本最后打印自检结果时，里面的 ✓ 会让 print
# 直接抛 UnicodeEncodeError —— 包已经打好了，却看着像失败了。
# 只把"这个编码写不出来的字符"换成 ?，中文照常显示。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "私人秘书"
# 默认打进 dist/；临时想打到别处（比如用户正开着 dist 里那份、锁着数据库），
# 就设 SECRETARY_BUILD_DIST=别的目录。这时**不会**去动桌面快捷方式 ——
# 那个必须指向用户真正在用的安装目录。
_OUT_ROOT = Path(os.environ.get("SECRETARY_BUILD_DIST") or (ROOT / "dist"))
DIST = _OUT_ROOT / APP_NAME
EXE = DIST / f"{APP_NAME}.exe"

# 用户自己的东西：重打包时会被 PyInstaller 连带删掉，所以要挪到一边再挪回来。
KEEP_ACROSS_BUILD = ("config.toml", "data", "plugins", "napcat")

# 版本号从代码里读（唯一来源是 app/version.py），不在这儿另写一份 ——
# 两个地方各写一遍，迟早对不上，而版本对不上就会拿错的代码去查问题。
sys.path.insert(0, str(ROOT))
from app.version import VERSION                                     # noqa: E402

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
    # 插件（plugins/*.py）是**运行时**才加载的，静态分析看不见它们的 import ——
    # 所以插件用到的标准库也得写在这儿，不然打包版一加载就 ModuleNotFoundError。
    # （2026-10-14：邮箱插件 import imaplib，打包版就是这么炸的；源码运行完全正常。）
    "imaplib",                            # 邮箱插件
    "email", "email.header", "email.utils", "email.message",
    "html",
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
        raise SystemExit(f"找不到图标：{icon}\n"
                         "（图标由 tools/make_icon.py 生成，先跑那个）")
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
        # 产物目录必须显式给：不给的话 PyInstaller 永远写 cwd 下的 dist/，
        # 上面那个 SECRETARY_BUILD_DIST 就只对我自己的后处理生效了（踩过）。
        "--distpath", str(_OUT_ROOT),
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
    # 图标单独放一份在 exe 旁边（带内容指纹的文件名）。
    # Windows 按**路径**缓存图标：exe 路径没变、只换了内容时，桌面会继续显示旧图。
    # 文件名一变，缓存必然失效 —— 见 desktop.launcher。
    meta = ROOT / "assets" / "icon.json"
    if meta.is_file():
        import json
        import shutil

        name = json.loads(meta.read_text(encoding="utf-8")).get("shortcut_icon")
        if name and (ROOT / "assets" / name).is_file():
            shutil.copy2(meta, DIST / "icon.json")
            shutil.copy2(ROOT / "assets" / name, DIST / name)


def verify() -> None:
    """打完之后自己验一遍。**不许只因为"命令跑完了"就说成功。**"""
    if not EXE.is_file():
        raise SystemExit(f"没生成 exe：{EXE}")

    # ⓪ 体积闸门。程序本体应该在 100 MB 上下；一旦有几百 MB，
    # 说明 EXCLUDES 那份名单里有东西又被拖进来了（表现是"安装包莫名其妙很大"）。
    # ⚠️ 只量**程序自己的文件**（exe + _internal）：自检排在"还原用户数据"之后，
    # 那时候 dist 里还躺着用户的 napcat（1.5 GB）—— 连它一起量，
    # 这道闸门会把正常构建误判成"有东西被拖进来了"（2026-10-13 真挡住了一次）。
    size_mb = sum(p.stat().st_size for p in (DIST / "_internal").rglob("*")
                  if p.is_file()) / 1024 / 1024
    size_mb += EXE.stat().st_size / 1024 / 1024
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
    if _OUT_ROOT != ROOT / "dist":
        print("（这次打在别处，跳过桌面快捷方式 —— 它该指向用户真正在用的那份）")
        return
    proc = subprocess.run([str(EXE), "--shortcut"], cwd=str(DIST),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120,
                          env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    print(((proc.stdout or "") + (proc.stderr or "")).strip())
    if proc.returncode != 0:
        raise SystemExit("快捷方式没建起来，看上面的报错")


README_TXT = """私人秘书 —— 用前必看

0. 双击没反应？多半是被杀毒软件隔离了（这个程序没签名，杀软常误报）。
   去「Windows 安全中心 → 病毒和威胁防护 → 保护历史记录」还原它，
   再把整个文件夹加进排除项。详细步骤看 README 的「被杀软隔离了怎么办」。

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

5. 收 QQ 消息要的那套东西（QQ 本体 + NapCat）也装在这个文件夹里，
   在 napcat\\ 下面，装完大约 1.5 GB（下载量约 300 MB，解开来就这么大，
   因为里面塞了一整个 QQ）。它跟你电脑上原本装的 QQ 是两份，互不影响；
   你想删掉它，就把整个文件夹删了 —— 不会在别处留东西。

6. 以后升级**不用重新安装**：拿到「补丁包-日期.zip」就关掉程序，
   把它解压到这个文件夹里覆盖，再打开就行。你的配置、待办、下好的 QQ 都不动。

7. 第一次打开，界面中间会有一张「还没弄完这几步」的清单，
   点哪一条就跳到对应的设置，跟着点完就能在手机上收到推送了。

8. 收 QQ 消息要么用小号、要么用大号：小号封了不心疼，大号群都加好了但风险
   落在主号上。作者自己用的是大号，自从用到现在没出过事 —— 这是经验，
   不是保证。本工具只收不发，不会主动发任何 QQ 消息。
"""


def app_is_running() -> bool:
    """程序还开着吗。

    为什么要专门查这个（2026-10-12 真踩过）：打包会先把用户自己的东西
    （config.toml / data / plugins / napcat）挪到一边，打完再挪回来。
    程序开着的时候数据库被锁住，`shutil.move` 会退化成"先复制、再删原件"，
    删到被锁的 secretary.db 就报错退出 —— 结果是**用户的数据被半路删掉一半**。
    与其赌运气，不如一开始就不让跑。
    """
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {APP_NAME}.exe", "/NH"],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return False                     # 查不动就不拦，别把打包本身卡死
    return APP_NAME in out


def napcat_is_running() -> bool:
    """NapCat 还在跑吗。

    2026-10-13 真踩过：这里原来只查 `私人秘书.exe`，没查 NapCat ——
    它是后台进程的子进程，后台停了它**可能还活着**，而且锁着 `napcat/` 里的文件。
    于是打包挪不动那份 1.5 GB 的目录：`shutil.move` 退化成"先复制、再删原件"，
    删到被锁的文件就报错退出，**用户下好的 QQ 被删掉了一半**
    （那次靠 `.keep` 里的完整副本还原回来了）。
    """
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq NapCatWinBootMain.exe", "/NH"],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "NapCatWinBootMain" in out


def stash_user_data(dist: Path | None = None) -> Path | None:
    """把用户自己的东西挪到 `dist/.keep/`，返回那个目录（没有东西可挪就 None）。

    PyInstaller 打包时会**删掉整个 `dist/私人秘书/`** —— 用户填好的 config.toml、
    待办附件、还有下好的 NapCat（约 1.5 GB）全在里面。作者自己就踩过：
    重打一次包，昨天刚装好的环境没了。

    同一块盘内改名是秒级的，1.5 GB 也不会真的复制一份。
    """
    dist = dist or DIST
    if not dist.is_dir():
        return None
    names = [name for name in KEEP_ACROSS_BUILD if (dist / name).exists()]
    if not names:
        return None
    hold = dist.parent / f".keep-{APP_NAME}"
    if hold.exists():
        shutil.rmtree(hold, ignore_errors=True)         # 上一次失败留下的
    if hold.exists():
        # 清不掉 = 还有东西占着里面的文件。硬来会把用户的数据删掉一半，
        # 不如直接停在这儿让他去关程序。
        raise SystemExit(
            f"清不掉上次留下的暂存目录：{hold}\n"
            "多半是还有程序占着里面的文件。把私人秘书（和后台）全部关掉再跑一次。")
    hold.mkdir(parents=True)
    for name in names:
        shutil.move(str(dist / name), str(hold / name))
    return hold


def restore_user_data(hold: Path | None, dist: Path | None = None) -> None:
    """把 `stash_user_data` 挪走的东西放回去。

    ⚠️ **同名时以用户那份为准**（2026-10-13 改）：
    以前这里是"目标已存在就跳过"，然后 `rmtree` 把用户那份一起删了 ——
    真丢过一次配置（自检那步在新目录里生成了一份默认 config.toml，
    于是用户那份被跳过并被删掉）。现在改成：把新的挪开，放回用户的。
    """
    if hold is None or not hold.is_dir():
        return
    dist = dist or DIST
    dist.mkdir(parents=True, exist_ok=True)
    for item in hold.iterdir():
        target = dist / item.name
        if target.exists():
            # 新打出来的那份是可再生的（默认配置 / 空库 / 自带插件），
            # 直接丢掉；用户的那份才是要留的。
            if item.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                try:
                    target.unlink()
                except OSError:
                    continue          # 删不掉（被占用）就别动，宁可留新的
        shutil.move(str(item), str(target))
    shutil.rmtree(hold, ignore_errors=True)


def write_readme() -> None:
    """在 exe 边上放一张"用前必看"。用普通文本，双击就能看，不挑软件。"""
    target = DIST / "用前必看.txt"
    target.write_text(README_TXT, encoding="utf-8-sig")
    print(f"已放好：{target.name}")


def make_patch_zip(dist: Path | None = None, full: bool = False) -> Path:
    """打一个**只含程序文件**的补丁包（exe + `_internal/`），放在 dist 外面。

    为什么要有这个（用户的诉求）：
    "以后像这样的升级能不能不重新安装，比如拖入补丁啥的直接重新设好，
    原本的配置不变。"

    整包（81 MB，解压后 81 MB + 1.5 GB NapCat）复制一遍很烦；
    而每次升级真正变的只有 `私人秘书.exe` 和 `_internal/` 里的东西。
    用户那边的升级动作就变成：**关掉程序 → 把补丁包解压到软件文件夹（覆盖）
    → 再打开**。`config.toml`、`data/`、`plugins/`、`napcat/` 都不在包里，
    所以配置、待办、下好的 QQ 一个都不会动。

    `full=True` 是给**第一次用的人**的整包：多放一份「用前必看.txt」。
    两个包的内容都不含 config/data/plugins/napcat —— 作者自己那份配置和待办
    就在 dist 目录里，绝不能跟着发出去。
    """
    import zipfile

    dist = dist or DIST
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    # 文件名带版本号：用户手里两份包时一眼看出版本，我拿到"我的补丁包叫
    # 补丁包-v0.1.0-…"也知道他升到哪儿了。build-info.txt 也塞进去 ——
    # 不然升完版本号还是旧的（它挨着 exe，不在 _internal 里）。
    # 补丁包带时间（同一天可能打两次），完整版带版本号就够
    name = f"完整版-v{VERSION}.zip" if full else f"补丁包-v{VERSION}-{stamp}.zip"
    target = dist.parent / name
    items = [dist / f"{APP_NAME}.exe", dist / "build-info.txt"]
    if full:
        items.append(dist / "用前必看.txt")
    internal = dist / "_internal"
    if internal.is_dir():
        items += [p for p in internal.rglob("*") if p.is_file()]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in items:
            if path.is_file():
                zf.write(path, path.relative_to(dist))
    return target


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
    text = f"v{VERSION}｜{stamp}｜提交 {commit or '未知'}"
    # 构建签名：给"这是不是我发的版本"留一个可对账的凭据。
    # 密钥只在我的机器上（环境变量），不进仓库 —— 源码自建出来的显示"未签名"。
    # 它防不了死心塌地改代码的人（本地校验永远能被绕过），但能做到两件事：
    #   ① 买家在「关于」里能看出自己拿到的是不是官方包；
    #   ② 反馈包里带签名，我一眼能认出"这不是我发的版本"。
    key = os.environ.get("SECRETARY_SIGN_KEY", "").strip()
    if key:
        import hashlib
        import hmac

        digest = hmac.new(key.encode("utf-8"), text.encode("utf-8"),
                          hashlib.sha256).hexdigest()[:16]
        text += f"｜签名 {digest}"
    else:
        text += "｜未签名（源码/自建）"
    (DIST / "build-info.txt").write_text(text, encoding="utf-8")
    print(f"已记下这一版：{text}")


def main() -> int:
    # 只在"确实要动用户那份安装"时才拦：设了 SECRETARY_BUILD_DIST 的话
    # 产物落在别处，用户开着他的程序也不冲突（锁着的是 dist 里那份数据库）。
    if _OUT_ROOT == ROOT / "dist" and app_is_running():
        raise SystemExit(
            f"{APP_NAME} 还开着。先把它关掉（界面右上角那个关闭按钮）再打包。\n"
            "开着的时候数据库被锁着，打包会把你自己的待办连带附件删掉一半 —— 真发生过。")
    if _OUT_ROOT == ROOT / "dist" and napcat_is_running():
        raise SystemExit(
            "NapCat 还在跑（后台停了它可能还活着），它锁着 napcat/ 里的文件。\n"
            "先把它收掉：打开程序 → 点「停止后台」；不行就任务管理器里结束\n"
            "NapCatWinBootMain.exe 和它那几个 QQ.exe。\n"
            "⚠️ 不关的话，打包挪那份 1.5 GB 的目录会失败，并且**会把你下好的 QQ 删掉一半**。")
    check_inputs()
    hold = stash_user_data()
    build()
    # ⚠️ 顺序很重要：**先把你自己的东西放回去，再跑自检**。
    # 这里写错了会真丢数据（2026-10-13 丢过一次配置）：
    # 自检是拿新 exe 跑一遍程序，它一看旁边没有 config.toml，
    # 就按"新用户"生成一份默认的 —— 然后还原那一步发现同名文件已存在，
    # 跳过并把**你那份**删掉了。
    restore_user_data(hold)
    verify()
    make_shortcut()
    write_readme()
    write_build_info()
    patch = make_patch_zip()
    full = make_patch_zip(full=True)
    # 一键升级包：把补丁塞进一个 exe，用户双击就升 —— 不用自己找文件夹解压。
    # 每个版本都出（用户要的"以后都这样"）。失败了不影响两个 zip，照常发版。
    upgrader = None
    try:
        subprocess.run([sys.executable, str(ROOT / "tools" / "build_upgrader.py"),
                        str(patch), str(_OUT_ROOT)], cwd=str(ROOT), check=True)
        upgrader = next(_OUT_ROOT.glob(f"私人秘书-一键升级-v{VERSION}*.exe"), None)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"（一键升级包没打出来：{type(exc).__name__}；两个 zip 不影响）")
    print()
    print("打包完成。软件就在这儿：")
    print(f"    {DIST}")
    print(f"（双击 {APP_NAME}.exe 就能用，桌面快捷方式已经建好了）")
    print(f"升级用的小包（~{patch.stat().st_size / 1048576:.0f} MB）：{patch.name}")
    print("    别人升级：把补丁包解压到软件文件夹里覆盖，配置和待办都不动。")
    print(f"第一次用的整包（~{full.stat().st_size / 1048576:.0f} MB）：{full.name}")
    print("    新用户下载这个。两个包都不含你的配置、待办和下好的 QQ。")
    if upgrader is not None:
        print(f"一键升级（~{upgrader.stat().st_size // 1048576} MB）：{upgrader.name}")
        print("    老用户双击它就行：关旧版 → 覆盖 → 重开，配置和待办不动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
