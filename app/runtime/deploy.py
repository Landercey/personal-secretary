"""一键部署：把 NapCat 装好并配上我们的地址。

=====================================================================
这整个文件是照着 2026-10-09 的真机验证写的，每一步都踩过
=====================================================================
结论：**用 NapCat 官方的 OneKey 安装器，不要自己下 QQ、不要注入系统里的 QQ。**

  1. 下 `NapCat.Shell.Windows.OneKey.zip`（0.9 MB）
  2. 解压，跑里面的 `NapCatInstaller.exe`
  3. 它自己下 QQ（腾讯官方 CDN 300 MB）+ NapCat（ghfast.top 镜像 28 MB），
     合并成一个自包含目录 `NapCat.XXXX.Shell/`
  4. 写 OneBot 配置指向我们
  5. **先起我们的接收端，再起 NapCat** ← 顺序反了就是"一直超时"

不需要：单独装 QQ、注入已安装的 QQ、改注册表、Sandboxie。删目录即卸载。

=====================================================================
三个坑（不处理就会重演）
=====================================================================
1. **二维码只活约 2 分钟，过期后 NapCat 不会自动刷新** —— 必须重启才有新码。
2. **配置要在第一次登录前写好**，或者写完后重启。NapCat 不热重载配置；
   首次登录它会用登录号新建一份**空白**配置，于是永远连不上。
   所以部署时必须先问用户"你要登哪个号"，把配置按那个号写好。
3. **我们的接收端必须先跑起来**。NapCat 是主动连过来的，连不上就每 15 秒
   无限重试，用户看到的是"一直超时" —— 昨天的根因就是这个。

=====================================================================
想改这里的话
=====================================================================
  换安装源       → onekey_url() / ONEKEY_MIRRORS
  改安装位置     → run.py 的 --dir
  改启动顺序     → 调用方（run.py 的 run_deploy）
  改配置内容     → write_onebot_config()
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

# 官方一键包。GitHub 直连在国内慢（实测 112 MB 要 455 秒），
# 但这个包只有 0.9 MB，直连可以接受。
ONEKEY_URL = (
    "https://github.com/NapNeko/NapCatQQ/releases/latest/download/"
    "NapCat.Shell.Windows.OneKey.zip"
)
# GitHub 连不上时的镜像。实测 ghfast.top 在国内可用。
ONEKEY_MIRRORS = (
    "https://ghfast.top/https://github.com/NapNeko/NapCatQQ/releases/latest/"
    "download/NapCat.Shell.Windows.OneKey.zip",
    "https://ghproxy.net/https://github.com/NapNeko/NapCatQQ/releases/latest/"
    "download/NapCat.Shell.Windows.OneKey.zip",
)

ONEBOT_PATH = "/onebot/v11/ws"

# 装的过程写在哪个文件里。
# 为什么要落文件：**装是后台进程在干，界面是另一个进程**（跟心跳一个套路），
# 不落文件的话界面只能干瞪眼，用户看到的就是"点了启动一直没反应"。
PROGRESS_NAME = "deploy.progress.json"


def progress_path() -> Path:
    from .config import default_data_dir

    return default_data_dir() / PROGRESS_NAME


def report(text: str, active: bool = True) -> None:
    """把"现在干到哪一步了"写下来给界面看。写不进去就算了，不能拖垮安装。"""
    path = progress_path()
    started = time.time()
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        if old.get("active") and old.get("started"):
            started = float(old["started"])      # 保留"这一轮从什么时候开始的"
    except (OSError, ValueError):
        pass
    payload = {"text": text, "active": active,
               "started": started, "at": time.time()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def read_progress() -> dict:
    """界面读进度。超过半小时没更新就当没在装（防止关窗口后留一条假的"正在装"）。"""
    try:
        data = json.loads(progress_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if time.time() - float(data.get("at") or 0) > 1800:
        return {}
    return data


def no_window_flags() -> int:
    """别弹黑框：装 QQ 的时候会蹦出终端窗口，用户会以为是病毒。

    ⚠️ 不能和 DETACHED_PROCESS 一起用（Windows 会直接报错），所以只给
    "我们自己不需要看它脸"的子进程加这个 —— 输出照样能抓到。
    """
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def download(url: str, dest: Path,
             on_progress: Callable[[str], None] | None = None,
             timeout: float = 120.0) -> Path:
    """下载一个文件。失败抛异常（由调用方决定怎么提示）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if on_progress:
        on_progress(f"下载 {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "personal-secretary"})
    chunks: list[bytes] = []
    got = 0
    next_mark = 25          # 每 25% 报一次，不然日志会被刷屏
    with urllib.request.urlopen(request, timeout=timeout) as response:
        total = int(response.headers.get("Content-Length") or 0)
        while True:
            block = response.read(64 * 1024)
            if not block:
                break
            chunks.append(block)
            got += len(block)
            if on_progress and total:
                percent = got * 100 // total
                if percent >= next_mark:
                    on_progress(f"  下载中 {percent}%"
                                f"（{got / 1048576:.1f}/{total / 1048576:.1f} MB）")
                    next_mark = percent + 25
    data = b"".join(chunks)
    dest.write_bytes(data)
    if on_progress:
        on_progress(f"  完成，{len(data) / 1024 / 1024:.1f} MB")
    return dest


def download_onekey(dest_dir: Path,
                    on_progress: Callable[[str], None] | None = None) -> Path:
    """下一键包，直连失败就换镜像。"""
    target = dest_dir / "NapCat.Shell.Windows.OneKey.zip"
    last_error: Exception | None = None
    for url in (ONEKEY_URL, *ONEKEY_MIRRORS):
        try:
            return download(url, target, on_progress)
        except Exception as exc:
            last_error = exc
            if on_progress:
                on_progress(f"  这个地址不行（{type(exc).__name__}），换下一个")
    raise RuntimeError(f"三个地址都下不下来：{last_error}")


def extract(zip_path: Path, dest_dir: Path,
            on_progress: Callable[[str], None] | None = None) -> Path:
    """解压一键包，返回解压目录。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if on_progress:
        on_progress(f"解压到 {dest_dir}")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)
    return dest_dir


def run_installer(onekey_dir: Path,
                  on_progress: Callable[[str], None] | None = None,
                  timeout: float = 1800.0) -> None:
    """跑官方安装器：它自己下 QQ + NapCat 并合并成一个目录。

    实测要几分钟（300 MB + 28 MB），超时给到 30 分钟。
    · 它在结束时等一个按键才退出，所以喂一个回车进去；
    · **窗口藏起来**（见 `no_window_flags`）—— 蹦黑框比慢更吓人；
    · **它自己不给进度条**，所以每 30 秒报一句"已经等了 X 分 Y 秒"，
      用户至少知道没卡死（有真进度可报的时候再报百分比，不糊弄）。
    """
    installer = onekey_dir / "NapCatInstaller.exe"
    if not installer.is_file():
        raise RuntimeError(f"一键包里没有安装器：{installer}")
    if on_progress:
        on_progress("运行官方安装器（它自己下 QQ 约 300 MB，这几分钟没有进度条，别关窗口）…")
    proc = subprocess.Popen(
        [str(installer)], cwd=str(onekey_dir),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=no_window_flags(),
    )

    def pump() -> None:
        """安装器自己吐出来的话转给调用方 —— 有就赚了，没有也不等它。"""
        for line in proc.stdout or []:
            line = line.strip()
            if line and on_progress:
                on_progress(f"  {line[:200]}")

    threading.Thread(target=pump, daemon=True).start()
    try:
        if proc.stdin:
            proc.stdin.write("\n")
            proc.stdin.flush()
    except OSError:
        pass

    started = time.time()
    told = 0.0
    while proc.poll() is None:
        waited = time.time() - started
        if waited > timeout:
            proc.kill()
            raise RuntimeError("安装器跑了超过 30 分钟还没结束，可能卡在下载上了")
        time.sleep(2)                       # 2 秒一次：装完了能立刻往下走
        if on_progress and waited - told >= 30:
            told = waited
            on_progress(f"  还在装…已经等了 {int(waited // 60)} 分"
                        f" {int(waited % 60)} 秒（它在下载，没卡住）")
    if proc.returncode not in (0, None) and on_progress:
        on_progress(f"  安装器退出码 {proc.returncode}")


def find_shell_dir(onekey_dir: Path) -> Path | None:
    """找安装出来的 `NapCat.XXXX.Shell` 目录（版本号会变，别写死）。"""
    if not onekey_dir.is_dir():
        return None
    found = sorted(onekey_dir.glob("NapCat.*.Shell"))
    return found[-1] if found else None


def config_dir(shell_dir: Path) -> Path:
    """NapCat 读配置的目录：`versions/<版本>/resources/app/napcat/config/`。"""
    found = sorted(shell_dir.glob("versions/*/resources/app/napcat/config"))
    return found[-1] if found else shell_dir / "config"


def write_onebot_config(shell_dir: Path, qq: int, token: str,
                        port: int = 18080) -> Path:
    """写好指向我们的 OneBot 配置。

    ⚠️ 文件名必须带 QQ 号（`onebot11_<QQ>.json`）。NapCat 按登录的号选配置文件；
    号对不上，它会自己新建一份**空的**，然后永远连不上我们。
    这是实测踩过的坑，所以部署时**必须先问用户要登哪个号**。
    """
    target_dir = config_dir(shell_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "network": {
            "httpServers": [],
            "httpClients": [],
            "websocketServers": [],
            "websocketClients": [{
                "name": "personal-secretary",
                "enable": True,
                "url": f"ws://127.0.0.1:{port}{ONEBOT_PATH}?access_token={token}",
                "messagePostFormat": "array",
                # 收自己发的消息 —— 小号收件箱（微信/短信截图转发）靠它
                "reportSelfMessage": True,
                "reconnectInterval": 15000,
                "token": token,
                "debug": False,
                "heartInterval": 30000,
            }],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
        "imageDownloadProxy": "",
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    target = target_dir / f"onebot11_{qq}.json"
    target.write_text(text, encoding="utf-8")
    return target


def qr_path(shell_dir: Path) -> Path:
    """NapCat 把登录二维码存哪儿。"""
    return config_dir(shell_dir).parent / "cache" / "qrcode.png"


def default_dir() -> Path:
    """NapCat 默认装哪儿：跟着数据目录走（绿色版就是程序旁边的 napcat/）。

    放在这里而不是 run.py，是因为**界面也要知道**（它要读二维码、要判断装没装）。
    界面不许 import run.py（那是唯一的装配点），所以这个路径必须由 runtime 提供。
    """
    from .config import default_data_dir

    return default_data_dir().parent / "napcat"


def launch_command(shell_dir: Path, qq: int | None = None) -> list[str]:
    """启动 NapCat 的命令行。

    带 QQ 号 = 尝试快速登录（实测首次仍要扫码，QQ 会要"手Q验证"）；
    不带 = 直接出二维码。
    """
    exe = shell_dir / "NapCatWinBootMain.exe"
    return [str(exe), str(qq)] if qq else [str(exe)]


def start_shell(shell_dir: Path, qq: int | None = None,
                log_path: Path | None = None):
    """起 NapCat。**返回 Popen**。窗口藏起来，但输出写进日志文件。

    为什么输出一定要有个去处（不能让它空着）：
      窗口藏掉之后子进程没有控制台，标准输出就是个空句柄。有些程序
      （NapCat 是 Node 写的）往里写会直接报错退出 —— 表现是"点启动，几秒后
      QQ 掉线"。写进文件就没事，而且**看得懂**：控制台那个窗口的编码不对，
      中文全是乱码，文件里是好的。
    """
    if log_path is None:
        log_path = default_dir() / "napcat.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sink = open(log_path, "a", encoding="utf-8", errors="replace")
    try:
        return subprocess.Popen(
            launch_command(shell_dir, qq), cwd=str(shell_dir),
            stdout=sink, stderr=subprocess.STDOUT,
            creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                           | no_window_flags()),
        )
    finally:
        sink.close()          # 父进程这边关掉，子进程自己留着那个句柄


def is_installed(onekey_dir: Path) -> bool:
    """装好了吗：有 Shell 目录、有启动器、有配置目录。"""
    shell = find_shell_dir(onekey_dir)
    if shell is None:
        return False
    return (shell / "NapCatWinBootMain.exe").is_file() and config_dir(shell).is_dir()
