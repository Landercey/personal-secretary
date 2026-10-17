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
import re
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


def report(text: str, active: bool = True, percent: int | None = None) -> None:
    """把"现在干到哪一步了"写下来给界面看。写不进去就算了，不能拖垮安装。"""
    path = progress_path()
    started = time.time()
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        if old.get("active") and old.get("started"):
            started = float(old["started"])      # 保留"这一轮从什么时候开始的"
    except (OSError, ValueError):
        pass
    payload = {"text": text, "active": active, "percent": percent,
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


def local_onekey(dest_dir: Path) -> Path | None:
    """用户自己放进来的离线包（别人发他的那份）。没有就 None。"""
    target = dest_dir / "NapCat.Shell.Windows.OneKey.zip"
    try:
        if target.is_file() and target.stat().st_size > 100 * 1024 \
                and zipfile.is_zipfile(target):
            return target
    except OSError:
        return None
    return None


def download_onekey(dest_dir: Path,
                    on_progress: Callable[[str], None] | None = None) -> Path:
    """下一键包，直连失败就换镜像。**本地已有就直接用，不联网。**

    为什么先看本地：有些校园网/安全软件会拦 HTTPS，Python 这边报的是
    `CERTIFICATE_VERIFY_FAILED`（浏览器却能打开，因为浏览器信了那个拦截证书）。
    这时候唯一可靠的办法就是别人把包发过来 —— 放在这个目录里，程序就用它。
    """
    target = dest_dir / "NapCat.Shell.Windows.OneKey.zip"
    if local_onekey(dest_dir) is not None:
        if on_progress:
            on_progress("用你放在这儿的离线包（这次不联网）")
        return target
    last_error: Exception | None = None
    # 两轮：换网（校园网 → 热点）的一瞬间，请求会被认证页接住，报的是证书错。
    # 等三秒再走一遍，比让用户自己"再点一次启动"可靠。
    for round_no in (1, 2):
        for url in (ONEKEY_URL, *ONEKEY_MIRRORS):
            try:
                return download(url, target, on_progress)
            except Exception as exc:
                last_error = exc
                if on_progress:
                    on_progress(f"  这个地址不行（{type(exc).__name__}），换下一个")
        if round_no == 1:
            if on_progress:
                on_progress("  网络像是刚断过 —— 等 3 秒再试一轮")
            time.sleep(3)
    raise RuntimeError(f"三个地址都下不下来：{last_error}{_offline_hint(last_error, dest_dir)}")


def _offline_hint(error: Exception | None, dest_dir: Path) -> str:
    """下载失败时补一句"到底为什么、还能怎么办"。

    证书错误和"被墙"是两回事：被墙是超时/重置，证书错误是**这台电脑不信任
    对方的证书**（校园网网关、公司代理、安全软件做 HTTPS 拦截时最常见）。
    不分开说，用户只会去换网络，换来换去还是这个错。
    """
    if "CERTIFICATE_VERIFY_FAILED" not in str(error or ""):
        return ""
    return ("\n  这是**证书**问题，不是被墙：这台电脑的 HTTPS 证书不被程序信任"
            "（校园网网关 / 公司代理 / 安全软件做 HTTPS 拦截时最常见）。"
            f"\n  办法一：让能装的人把整个 napcat 文件夹打包发你，解压到 {dest_dir.parent}"
            f"\n  办法二：下载 OneKey 包，改名成 NapCat.Shell.Windows.OneKey.zip，"
            f"放进 {dest_dir}，再点一次「启动 QQ」")


def extract(zip_path: Path, dest_dir: Path,
            on_progress: Callable[[str], None] | None = None) -> Path:
    """解压一键包，返回解压目录。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if on_progress:
        on_progress(f"解压到 {dest_dir}")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)
    return dest_dir


def read_percent(line: str) -> int | None:
    """从安装器那行输出里抠出百分比。抠不出来返回 None。

    实测它打的是这种（中文按 GBK 输出，读串了就是乱码）：

        下载 QQ 26% [<======>] (78MB/300MB)

    所以两条路都试：先找 `26%`，没有就按 `78MB/300MB` 自己算。
    """
    text = str(line or "")
    found = re.search(r"(\d{1,3})\s*%", text)
    if found:
        return max(0, min(100, int(found.group(1))))
    size = re.search(r"(\d+(?:\.\d+)?)\s*MB\s*/\s*(\d+(?:\.\d+)?)\s*MB", text)
    if size:
        done, total = float(size.group(1)), float(size.group(2))
        if total > 0:
            return max(0, min(100, int(done * 100 / total)))
    return None


def decode_output(raw: bytes) -> str:
    """把子进程吐的一行字节变成字。

    为什么要自己判：这些第三方 exe 在中文 Windows 上按 **GBK** 输出，
    我们按 UTF-8 读就会得到一屏"�"（用户拍过照片来报这个）。
    先按 UTF-8 严格试，不行再按 GBK，都不行才用替换字符兜底。
    """
    for codec in ("utf-8", "gbk"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# NapCat 的报错 → 人话。它自己说得很简略（"已登录,无法重复登录"），
# 用户看完不知道"已经在哪登录了、我该干什么"。
NAPCAT_ERROR_HINTS = (
    ("已登录",
     "这个 QQ 号已经在别处登着了 —— 多半是你自己电脑上那个 QQ 正登着它。"
     "同一个号不能同时登两个电脑端：要么在那边退出登录，要么换一个号给这个工具用"
     "（手机上的那个不算冲突，可以继续登着）。"),
    ("二维码", "二维码过期了，点「重新出码」换一张。"),
)

# NapCat 的日志给级别套了**颜色转义码**：`[<ESC>[31merror<ESC>[39m]`。
# 不先剥掉的话按 "[error]" 找一行都找不到 —— 真踩过：单测按"我以为的格式"
# 写是绿的，拿真日志一跑什么都读不出来。
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def napcat_error_hint(onekey_dir: Path | None = None) -> str:
    """从 NapCat 自己的日志里捞最后一条报错，翻成人话（认不出来就原样给）。

    为什么要读它的日志：登录失败时 NapCat 只在自己日志里写一句，
    **界面上一点都看不出来** —— 用户看到的就是"扫了码、点了确认、然后什么都没有"
    （2026-10-13 报的，实际原因是那个号正登在他自己电脑上的 QQ 里）。
    """
    path = (onekey_dir or default_dir()) / "napcat.log"
    try:
        blob = path.read_bytes()[-8000:]        # 只读尾巴：日志会一直长
    except OSError:
        return ""
    text = _ANSI.sub("", decode_output(blob))
    lines = [ln.strip() for ln in text.splitlines() if "[error]" in ln]
    if not lines:
        return ""
    last = lines[-1].split("[error]", 1)[-1].strip()
    for needle, hint in NAPCAT_ERROR_HINTS:
        if needle in last:
            return hint
    return last[:120]                            # 认不出来也给原话，用户至少能搜


def run_installer(onekey_dir: Path,
                  on_progress: Callable[..., None] | None = None,
                  timeout: float = 1800.0) -> None:
    """跑官方安装器：它自己下 QQ + NapCat 并合并成一个目录。

    实测要几分钟（300 MB + 28 MB），超时给到 30 分钟。
    · 它在结束时等一个按键才退出，所以喂一个回车进去；
    · **窗口藏起来**（见 `no_window_flags`）—— 蹦黑框比慢更吓人；
    · **它其实报进度**（`26% [<====>] (78MB/300MB)`），只是中文按 GBK 输出、
      不看清楚就漏过去了。所以这里既转它的话，也把百分比抠出来给界面画进度条；
      抠不出来的阶段，每 30 秒报一句"已经等了 X 分 Y 秒"兜底。
    """
    installer = onekey_dir / "NapCatInstaller.exe"
    if not installer.is_file():
        raise RuntimeError(f"一键包里没有安装器：{installer}")
    if on_progress:
        on_progress("运行官方安装器（它自己下 QQ 约 300 MB，别关窗口）…")
    proc = subprocess.Popen(
        [str(installer)], cwd=str(onekey_dir),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # 读**字节**、自己判编码：第三方 exe 在中文系统上按 GBK 吐字，
        # 交给 Popen 按 UTF-8 解就是一屏乱码（用户报过）。
        text=False,
        creationflags=no_window_flags(),
    )

    def pump() -> None:
        """安装器自己吐出来的话转给调用方 —— 有就赚了，没有也不等它。"""
        for raw in proc.stdout or []:
            line = decode_output(raw).strip()
            if not line or not on_progress:
                continue
            percent = read_percent(line)
            if percent is None:
                on_progress(f"  {line[:200]}")
            else:
                on_progress(f"  {line[:200]}", percent)

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
        # ⚠️ 必须 True（2026-10-16 改）：班群里"班长把通知合并转发"是最常见的形态，
        # 而这个开关关着的时候，NapCat 只给一个 [合并转发] 占位符 —— 里面的字
        # 一个字都拿不到，整包通知被判成闲聊扔进废件箱（用户看到的正是这个）。
        # 打开之后节点内容会一起带下来，程序那边 `forward_text()` 负责认。
        "parseMultMsg": True,
        "imageDownloadProxy": "",
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    target = target_dir / f"onebot11_{qq}.json"
    target.write_text(text, encoding="utf-8")
    return target


def qr_path(shell_dir: Path) -> Path:
    """NapCat 把登录二维码存哪儿。"""
    return config_dir(shell_dir).parent / "cache" / "qrcode.png"


def onebot_config_ok(shell_dir: Path, qq: int, token: str, port: int) -> bool:
    """我们写的那份 OneBot 配置还在不在、还是不是我们写的。

    **为什么必须盯着它**（用户报的"扫码登录成功后还要再点一次启动 QQ"）：
    首次登录时 NapCat 会用登录号**重建一份空白配置**，把我们写好的那份覆盖掉 ——
    它自己那份没有指向我们的地址，于是永远连不上；用户再点一次「启动 QQ」，
    我们又写一遍，这次它读到了才通。
    所以部署循环里要一直看这份文件：字被换掉了就重写 + 重启一次。
    """
    target = config_dir(shell_dir) / f"onebot11_{qq}.json"
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return token in text and f":{port}" in text


def clear_qr(shell_dir: Path) -> None:
    """删掉上一张二维码。重启后台之前调用。

    不删会同时出两个毛病（用户报的）：
      ① 界面上继续显示一张已经作废的码，用户扫它当然登不上；
      ② 二维码的"年龄"是按文件修改时间算的 —— 刚重启完就显示
         "已经 5 分钟了，多半过期了"，用户只会觉得程序在胡说。
    """
    try:
        qr_path(shell_dir).unlink()
    except OSError:
        pass          # 本来就没有，正常（第一次装）


def default_dir() -> Path:
    """NapCat 默认装哪儿：跟着数据目录走（绿色版就是程序旁边的 napcat/）。

    放在这里而不是 run.py，是因为**界面也要知道**（它要读二维码、要判断装没装）。
    界面不许 import run.py（那是唯一的装配点），所以这个路径必须由 runtime 提供。
    """
    from .config import default_data_dir

    return default_data_dir().parent / "napcat"


def launch_command(shell_dir: Path, qq: int | None = None) -> list[str]:
    """启动 NapCat 的命令行。

    带 QQ 号 = **快速登录**：NapCat 用本机存着的那次登录态直接登进去，
    **不用再扫码**（它的日志里写着"正在快速登录 123456789"）。
    不带 = 直接出二维码，每次都等用户扫 —— 2026-10-13 用户报
    "频繁登录太麻烦"，根因就是这个参数没传（见 run.py 的 run_deploy）：
    重启后台、关机再开、更新完，都白白让他重扫一次。

    首次登录仍然要扫码（本机还没有登录态）；快速登录失败（比如那个号
    正登在他自己电脑的 QQ 上）时 NapCat 会直接退出，由
    `fast_login_failed()` 认出来并退回扫码。
    """
    exe = shell_dir / "NapCatWinBootMain.exe"
    return [str(exe), str(qq)] if qq else [str(exe)]


def fast_login_failed(proc, connected: bool) -> bool:
    """这次快速登录没成吗：**进程已经退了、而且还没连上**。

    为什么能这么判：成功时那个启动器进程会一直活着（真实进程树里
    NapCatWinBootMain → node → 一堆 QQ.exe），失败时它印完
    "已登录,无法重复登录" 就自己退出。
    """
    return not connected and proc is not None and proc.poll() is not None


def should_restart_for_login(offline_seconds: float, *, minutes: int,
                             used: int, cap: int, proc_alive: bool,
                             connected: bool) -> bool:
    """连不上这么久、但 NapCat 进程还活着 —— 该带号重启一次吗？

    2026-10-16 真事：早上 06:30 网络抖了一下，登录态掉了，NapCat 退回要二维码、
    每 2 分钟报一次 `Login Error, ErrType 1 ErrCode 3`，可是**它进程没退** ——
    所以 `fast_login_failed()` 判不出来（那条要求进程已退出），程序就一直干等，
    非要用户手动点「重新启动 QQ」才恢复（用户原话："早上还要点一次重新启动 QQ"）。

    这里补的就是那一步：等够时间自己重启一次（重启会重试快速登录，不用扫码）。
    三个闸缺一不可 —— 连上了不重启、进程死了不管（交给 fast_login_failed）、
    `minutes <= 0` 表示用户关了这功能、**重启次数有上限**（反复登录既没用又招风控）。
    """
    if connected or not proc_alive or minutes <= 0 or used >= cap:
        return False
    return offline_seconds >= minutes * 60


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


def _image_paths() -> list[tuple[int, str]]:
    """这台机器上每个进程的 (PID, exe 路径)。只用 Windows 自带接口，不引第三方库。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    capacity = 8192
    pids = (wintypes.DWORD * capacity)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
        return []
    buf = ctypes.create_unicode_buffer(32768)
    out: list[tuple[int, str]] = []
    for pid in pids[:needed.value // ctypes.sizeof(wintypes.DWORD)]:
        if not pid:
            continue
        handle = kernel32.OpenProcess(0x1000, False, pid)      # QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                out.append((int(pid), buf.value))
        finally:
            kernel32.CloseHandle(handle)
    return out


def stray_processes(shell_dir: Path) -> list[int]:
    """还活着、但**属于这个 NapCat 目录**的进程。

    为什么不能只靠 `stop_shell(proc)`：那个启动器把 QQ 拉起来之后自己会退出，
    于是 `proc.poll() is not None` → 直接 return，底下那一窝 QQ 全成了孤儿。
    这台机器上实测堆到 23 个启动器 + 92 个 QQ.exe —— 每个都在抢同一个号的登录，
    表现就是"启动很久没反应""扫了码还是没登上""后台连不上"。

    只按**路径**认（`<shell_dir>\\...`），绝不按进程名杀：
    用户电脑上自己的 QQ 也叫 QQ.exe，按名字杀等于把人家的聊天软件关了。
    """
    try:
        root = str(Path(shell_dir).resolve()).lower().rstrip("\\")
    except OSError:
        return []
    if not root:
        return []
    import os

    me = os.getpid()
    return [pid for pid, path in _image_paths()
            if pid != me and path.lower().startswith(root + "\\")]


def stop_all(shell_dir: Path) -> int:
    """把这个目录下的 NapCat / QQ 全部收掉，返回清掉了几个。

    ⚠️ 必须**一次交一批**给 taskkill：一条一条调的话，每个都要等它真的退出，
    实测 115 个进程要磨两分钟 —— 那就又变回"点了启动一直在等"。
    """
    killed = 0
    for _ in range(3):                       # 有的 QQ 会 fork 出新进程，来两轮才干净
        pids = stray_processes(shell_dir)
        if not pids:
            break
        for start in range(0, len(pids), 100):
            args = ["taskkill", "/F", "/T"]
            for pid in pids[start:start + 100]:
                args += ["/PID", str(pid)]
            try:
                subprocess.run(args, capture_output=True, timeout=30,
                               creationflags=no_window_flags())
            except Exception:
                pass
            killed += len(pids[start:start + 100])
        time.sleep(0.8)
    return killed


def stop_shell(proc, shell_dir: Path | None = None) -> None:
    """把 NapCat **连着它的子进程**一起收掉。

    ⚠️ 只 terminate 那个启动器是不够的（2026-10-13 实测）：真正的 QQ 是它的
    **子进程**（这台机器上二十多个 QQ.exe），Windows 不会连带杀 ——
    结果就是"停止后台"之后还剩一大堆 QQ 在跑：占一个多 G 内存，
    而且**锁着 napcat/ 目录**（打包时要挪它，挪不动就会半路删掉一半文件）。
    用 taskkill /T 连整棵树一起收。
    """
    if proc is not None and proc.poll() is None:
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=20,
                           creationflags=no_window_flags())
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    # 启动器早就退了也一样要扫一遍：它的子进程还在（见 stray_processes 的注释）。
    if shell_dir is not None:
        stop_all(shell_dir)


def is_installed(onekey_dir: Path) -> bool:
    """装好了吗：有 Shell 目录、有启动器、有配置目录。"""
    shell = find_shell_dir(onekey_dir)
    if shell is None:
        return False
    return (shell / "NapCatWinBootMain.exe").is_file() and config_dir(shell).is_dir()
