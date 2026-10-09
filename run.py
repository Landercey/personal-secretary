"""开发期命令行入口。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  加一个新命令                    → 在 `main()` 里加一个 argparse 参数和分支
  改零件怎么装配（注册新插件）    → `build()` —— 全项目只有这里能 import 各模块
  改录入之后做什么（比如顺手推送） → `ingest()`
  改自检检查哪几项                → `_selftest()`

**这个文件是全项目唯一的"装配点"。** 别的模块之间不许互相 import
（由 tests/test_contract_integrity.py 自动检查）。想加一个新零件，
就是在这里 `new` 出来、塞进 Pipeline。

=====================================================================
第 9 步会被图形界面取代，但在那之前它是验证链路唯一的工具 ——
而且它是**唯一**一个允许 import 各模块来组装程序的文件（装配点），
别的模块之间不许互相 import。

用法：
    python run.py "明天下午三点交作业"     录入一条
    python run.py --file 作业.pptx         把一个文件变成待办（读里面的字）
    python run.py --read 照片.jpg          只看这个文件能提出什么字，不入库
    python run.py --list                   看未完成
    python run.py --list junk              看废件箱
    python run.py --done 3                 标记完成
    python run.py --digest                 生成一份汇总并打印到控制台
    python run.py --push                   把汇总真正推送到企业微信
    python run.py --check-push             检查推送通道配置对不对（不发消息）
    python run.py --schedule               启动定时推送（早报/晚间，Ctrl+C 停止）
    python run.py --next-push              看下一次什么时候推
    python run.py --check-qq               看 QQ 接入的配置和状态
    python run.py --qq                     启动 QQ 接收（Ctrl+C 停止）
    python run.py --deploy --account <QQ号>  一键装 NapCat 并启动（先接收端后 QQ）
    python run.py --stats                  看统计
    python run.py --plugins                看装了哪些插件、有没有坏掉的
    python run.py --sync                   拉一遍插件来源（课表订阅），变成待办
    python run.py --selftest               检查契约与环境
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.contracts.models import STATUS_DONE, STATUS_DROPPED     # noqa: E402
from app.pipeline.chain import Pipeline                          # noqa: E402
from app.pipeline.chat_filter import ChatNoiseEnricher           # noqa: E402
from app.pipeline.file_enricher import FileEnricher              # noqa: E402
from app.pipeline.parse_enricher import RuleParseEnricher        # noqa: E402
from app.runtime.config import config, default_data_dir          # noqa: E402
from app.runtime.plugins import (                                # noqa: E402
    Registry, load as load_plugins, plugin_dir,
)
from app.runtime.scheduler import Scheduler, due_slots           # noqa: E402
from app.runtime.sysmon import describe as mem_describe          # noqa: E402
from app.sinks.console import build_digest                       # noqa: E402
from app.sinks.router import build_sink, channel_status          # noqa: E402
from app.sources.manual import ManualSource                      # noqa: E402
from app.sources.qq import QQSettings, QQSource, ensure_token    # noqa: E402
from app.store.repository import SqliteStore, open_database      # noqa: E402


def build(database: str | Path | None = None):
    """把零件装配起来。换数据库路径主要是为了测试。

    加一个新零件/插件的标准动作就是改这里（下面四行）：
        store / source / pipeline / sink
    比如以后接 QQ 来源，就是在 source 那边多一个 QQSource；
    加 AI 理解，就是往 plugins/ 丢一个插件（不用改这个文件）。

    插件的加载放在装配的最后一步：**坏插件只会被记一笔**，
    不影响这里把主程序装配起来（CONTRACT.md 第二节硬性约束）。
    返回值里多带的 registry 就是给 --plugins / --sync / 界面 / 自检看的。
    """
    root = Path(__file__).resolve().parent
    db_path = Path(database) if database else default_data_dir() / "secretary.db"
    store = SqliteStore(open_database(db_path))
    source = ManualSource()

    registry = Registry()
    load_plugins(root, registry)     # 加载结果记在 registry.status 上

    # 理解层的零件分两组，管线保证「消息级」整组先跑：
    #   ① 文件解析（把附件读成文字拼进正文）
    #   ② 闲聊过滤 → 切分/时间识别 →（以后）去重 → AI
    # 为什么文件解析必须在最前：正文可能只有"看这个"，真正的活在图里。
    # 插件注册进来的零件按各自的 `order` 自动插到位置上（AI 是 90，排最后）。
    pipeline = Pipeline(
        text_enrichers=[FileEnricher(), *registry.text_enrichers],
        draft_enrichers=[
            ChatNoiseEnricher(),
            RuleParseEnricher(),
            *registry.draft_enrichers,
        ],
    )
    sink = build_sink()          # 配了企微就走企微，没配就退回控制台
    return store, source, pipeline, sink, registry


def ingest_message(store, pipeline, message) -> int:
    """一条标准消息 → 待办。返回新入库的条数。

    整条流水线一共五步（QQ 和手动录入走的是同一条）：
      1. 存原始消息（留证据，出问题能回查"这条到底收没收到"）
      2. 理解层跑一遍：消息 → 草稿
      3. 草稿入库（重复的会被自动挡掉）
      4. 被判为闲聊/碎片的，记进废件箱（不推送，但留证据）
      5. 顺手清掉过期记录
    """
    store.save_message(message)
    prepared, drafts = pipeline.run(message)
    # 再存一次。第一次存的是"还没读附件"的原样消息，跑完管线才有正文 ——
    # 只存第一次的话，文件类消息在库里永远是空的，
    # "这条为什么没收下"就查不出来了。save_message 是 INSERT OR REPLACE，
    # 重复调用没副作用。
    store.save_message(prepared)
    stored = store.save_drafts(prepared, drafts)

    # 合并窗口：这条自己没解析出待办，但带着附件 ——
    # 大概率是"上一条的补充材料"（先发一句"明天要交作业"，
    # 紧接着发一张作业要求图）。挂到刚才那条上，别变成孤零零一条。
    if not drafts and prepared.attachments:
        window = int(config.get("parse", "merge_window_seconds", 60) or 0)
        recent = store.recent_task_for(prepared.source_kind, prepared.source_name,
                                       prepared.sender, window)
        if recent is not None:
            attached = store.attach_message_files(recent.task_id, prepared)
            print(f"  ↳ 合并到刚才那条「{recent.title}」（挂了 {attached} 个附件）")
            store.purge_old_dropped()
            return 0

    # 被闲聊过滤器挡下的
    skip_reason = prepared.raw.get("skip_reason")
    if skip_reason and not drafts:
        store.log_dropped_text(prepared.text or "", f"闲聊：{skip_reason}",
                               prepared.source_kind, prepared.source_name, prepared.msg_id)

    # 被判为碎片/噪声的行记一笔：不推送，但留证据。
    # 由解析器告诉我们具体是哪几行（见 RuleParseEnricher._record_dropped），
    # 装配点只负责落库，不自己猜。
    for line in prepared.raw.get("dropped_lines", []):
        store.log_dropped_text(line, "碎片：摘掉时间后没内容",
                               prepared.source_kind, prepared.source_name, prepared.msg_id)
    store.purge_old_dropped()
    return len(stored)


def ingest(store, source, pipeline, text: str) -> int:
    """一段文字 → 待办（手动录入的快捷方式）。"""
    return ingest_message(store, pipeline, source.from_text(text))


def explicit_file_message(source, path: str):
    """界面上拖进来、用户点着「变成待办」的文件。

    和 `--file` 的区别只有一处：这条打上 `explicit` 标记。
    用户已经点着按钮表过态了，理解层就不该再拿"文件里大多是资料"
    去替他判断 —— 那等于点了按钮还是没反应。
    """
    message = source.from_files([str(path)])
    message.raw["explicit"] = True
    return message


def extract_preview(path: str) -> dict:
    """只把文件里的字读出来，不入库（界面上「只看写了什么」用）。

    连**为什么没读出来**一起返回。只回一个空字符串的话，用户看到的
    永远是"这个文件里没读出文字"，分不清是扫描件、旧版 Office、
    还是不支持的格式 —— 那他就只能来问作者。
    """
    from app.pipeline.extract import extract_text

    target = Path(path)
    if not target.is_file():
        return {"ok": False, "text": "", "note": "文件不在了", "method": ""}
    result = extract_text(target)
    return {"ok": bool(result.ok), "text": result.text or "",
            "note": result.note or "", "method": result.method or ""}


def read_file(path: str) -> int:
    """只看一个文件能提出什么文字，不入库。

    调这个是为了回答"这张图/这份 PPT 到底被读成了什么" ——
    排查"为什么它没变成待办"时的第一个动作。
    """
    from app.pipeline.extract import extract_text, job_type_for
    from app.pipeline.extract.ocr import describe as ocr_describe

    target = Path(path)
    if not target.is_file():
        print(f"文件不存在：{target}")
        return 1
    kind = job_type_for(target)
    print(f"文件：{target.name}（{target.stat().st_size // 1024} KB）")
    print(f"类型：{kind or '不认识这种格式'}")
    print(f"引擎：{ocr_describe()}")
    print("-" * 62)
    result = extract_text(target)
    print(f"结果：{'成功' if result.ok else '失败'}｜{result.method}｜{result.note}")
    print("-" * 62)
    print(result.text[:2000] if result.text else "（没有文字）")
    return 0 if result.ok else 1


def run_qq(store, pipeline) -> int:
    """启动 QQ 接收，一直跑到用户按 Ctrl+C。

    为什么放在这里而不是 app 里：起服务、接线、打印状态是"装配"的活，
    全项目只有 run.py 负责装配。
    """
    settings = QQSettings.from_config()
    if not settings.enabled:
        print("QQ 接入没有启用。")
        print("打开 config.toml，把 [qq] 下面的 enabled 改成 true，再运行一次。")
        return 1

    token = ensure_token(settings.token)
    if token != settings.token:
        print("⚠️ [qq] token 还是空的。config.toml 里要填这个（程序已生成）：")
        print(f"   token = \"{token}\"")
        print("   把上面这行贴进 config.toml 的 [qq] 段，再重启一次。")
        return 1

    def on_message(message) -> None:
        added = ingest_message(store, pipeline, message)
        mark = "✓ 入待办" if added else "· 已看过"
        print(f"  {mark}│ {message.source_name}│ {message.text[:40]}")

    def on_status(state) -> None:
        if state.get("error"):
            print(f"  ⚠️ {state['error']}")

    def on_reject(reason: str, preview: str) -> None:
        """被拦下的消息也要留痕迹。

        用户发了一条，程序这边什么都不显示，是最难排查的情况 ——
        所以这里既打印出来，也写进废件箱（靠 `--list junk` 翻得到）。
        """
        print(f"  ✗ 拦下│ {reason}│ {preview[:34]}")
        store.log_dropped_text(preview, f"QQ 拦下：{reason}",
                               "qq", "", "")

    source = QQSource(settings=settings, on_message=on_message,
                      on_status=on_status, on_reject=on_reject)
    if not source.start():
        print("启动失败，看上面的提示。")
        return 1

    print("=" * 62)
    print("  私人秘书 · QQ 接收")
    print("=" * 62)
    print(f"  监听：       {settings.bind_host}:{settings.bind_port}{settings.path}")
    print(f"  接收范围：   {settings.summary()}")
    print(f"  NapCat 需要填的地址：")
    print(f"      ws://{settings.bind_host}:{settings.bind_port}{settings.path}"
          f"?access_token={settings.token}")
    print()
    print("  现在去 NapCat 里把上面这个地址配好，然后等它连上来。")
    print("  按 Ctrl+C 停止。")
    print("=" * 62)
    # 只在数字变化时打一行。每 2 秒重打一遍同样的内容，会把真正有用的
    # 那几行（收到、拦下）冲到看不见的地方。
    last_line = ""
    try:
        while True:
            time.sleep(2)
            state = source.status()
            if not state.get("running"):
                continue
            if state.get("connected"):
                line = (f"  [已连接] 登录号 {state.get('self_id') or '?'}｜"
                        f"收到 {state.get('received', 0)}｜收下 {state.get('accepted', 0)}｜"
                        f"拦下 {state.get('skipped', 0)}"
                        + (f"｜最后一条被拦原因：{state['last_reason']}"
                           if state.get("last_reason") else ""))
            else:
                line = "  [等待连接] 等 NapCat 连上来…"
            if line != last_line:
                print(line)
                last_line = line
    except KeyboardInterrupt:
        print("\n  停止中…")
    finally:
        source.stop()
    print("  已停止。")
    return 0


def run_scheduler(store, sink) -> int:
    """跑定时推送，一直跑到 Ctrl+C。"""
    scheduler = Scheduler(store, sink, on_event=lambda text: print(f"  {text}"))
    if not scheduler.enabled():
        print("定时推送已关闭（config.toml 的 [push] enabled = false）。")
        return 1

    planned = parse_times_or_empty()
    if not planned:
        print("没有配置任何推送时间点。")
        print("打开 config.toml，在 [push] times 里写，例如 [\"07:30\", \"21:30\"]。")
        return 1

    print("=" * 62)
    print("  私人秘书 · 定时推送")
    print("=" * 62)
    print(f"  {scheduler.describe()}")
    print(f"  {mem_describe()}")
    print("  按 Ctrl+C 停止。")
    print("=" * 62)

    # 启动时先把错过的补上（在补推窗口内的才补）
    for batch in scheduler.run_once():
        print(f"  [补推] {batch}")

    scheduler.start()
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n  停止中…")
    finally:
        scheduler.stop()
    print("  已停止。")
    return 0


def parse_times_or_empty() -> list[str]:
    """配置里的推送时间点，规范化后的列表。"""
    from app.runtime.scheduler import parse_times

    return parse_times(config.get("push", "times", []))


def deploy_dir() -> Path:
    """NapCat 装哪儿。跟着数据目录走。"""
    return default_data_dir().parent / "napcat"


def run_setup(onekey_dir: Path, account: int, token: str, port: int) -> int:
    """只装不启动：下包 → 跑官方安装器 → 写我们的配置。"""
    from app.runtime import deploy

    onekey_dir.mkdir(parents=True, exist_ok=True)
    if deploy.is_installed(onekey_dir):
        print("已经装过了，跳过下载安装。")
    else:
        try:
            zip_path = deploy.download_onekey(onekey_dir, print)
            deploy.extract(zip_path, onekey_dir, print)
            deploy.run_installer(onekey_dir, print)
        except Exception as exc:
            print(f"\n安装失败：{exc}")
            print("可以手动去 https://github.com/NapNeko/NapCatQQ/releases "
                  "下载 OneKey 包，解压后双击 NapCatInstaller.exe。")
            return 1

    shell = deploy.find_shell_dir(onekey_dir)
    if shell is None:
        print(f"没找到装出来的 Shell 目录（在 {onekey_dir} 下面找 NapCat.*.Shell）")
        return 1

    config_path = deploy.write_onebot_config(shell, account, token, port)
    print()
    print("=" * 62)
    print("  装好了")
    print("=" * 62)
    print(f"  安装目录：{shell}")
    print(f"  配置文件：{config_path}")
    print(f"  登录账号：{account}（配置按这个号写的，换了号要重新写）")
    print(f"  连接地址：ws://127.0.0.1:{port}/onebot/v11/ws")
    print()
    print("  要跑起来：python run.py --deploy")
    print("=" * 62)
    return 0


def run_deploy(store, pipeline, account: int) -> int:
    """一键：装（如需要）→ 起接收端 → 起 NapCat。

    ⚠️ 顺序是死的：**我们的接收端必须先监听**。
    NapCat 是主动连过来的，连不上就每 15 秒无限重试，
    用户看到的是"一直超时"（昨天卡在这里一整晚）。
    """
    from app.runtime import deploy

    onekey_dir = deploy_dir()
    token = ensure_token(str(config.get("qq", "token", "") or ""))
    port = int(config.get("qq", "bind_port", 18080) or 18080)

    if not deploy.is_installed(onekey_dir):
        code = run_setup(onekey_dir, account, token, port)
        if code != 0:
            return code

    shell = deploy.find_shell_dir(onekey_dir)
    assert shell is not None
    deploy.write_onebot_config(shell, account, token, port)

    # ① 先起我们的接收端
    settings = QQSettings.from_config()
    if not settings.enabled:
        print("QQ 接入没启用（config.toml 的 [qq] enabled）。")
        return 1
    source = QQSource(settings=settings,
                      on_message=lambda m: ingest_message(store, pipeline, m),
                      on_status=lambda s: s.get("error") and print(f"  ⚠️ {s['error']}"))
    if not source.start():
        print("接收端没起来，先别启动 NapCat。")
        return 1
    print(f"  接收端已监听 127.0.0.1:{port}")

    # ② 再起 NapCat
    napcat = subprocess.Popen(deploy.launch_command(shell),
                              cwd=str(shell),
                              creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    print(f"  NapCat 已启动（PID {napcat.pid}）")

    print()
    print("=" * 62)
    print("  即将弹出登录二维码，用手机 QQ 扫它")
    print(f"  二维码图片：{deploy.qr_path(shell)}")
    print("  ⚠️ 二维码约 2 分钟过期，过期了 Ctrl+C 重跑一次就好")
    print("=" * 62)

    last_line = ""
    try:
        while True:
            time.sleep(2)
            state = source.status()
            if state.get("connected"):
                line = (f"  [已连接] 登录号 {state.get('self_id') or '?'}｜"
                        f"收到 {state.get('received', 0)}｜收下 {state.get('accepted', 0)}｜"
                        f"拦下 {state.get('skipped', 0)}")
            else:
                line = "  [等 NapCat 连上来] 扫上面的二维码…"
            if line != last_line:
                print(line)
                last_line = line
    except KeyboardInterrupt:
        print("\n  停止中…")
    finally:
        source.stop()
        if napcat.poll() is None:
            napcat.terminate()
    return 0


def run_ui(store, plugins=(), actions=None) -> int:
    """打开界面窗口。

    ⚠️ 界面和"收消息"是**两个独立进程**，共用同一个 SQLite 库。
    好处：界面关掉不影响后台收消息；后台也不用为了显示而常驻一个窗口。
    这也解释了为什么值得用网页界面 —— 那 400 多 MB 只在窗口开着时占。

    `actions` 是界面要用的"理解层动作"（手动录入、拉课表）。
    用参数递进去而不是让界面 import 那些模块 —— 全项目只有这里能装配。
    """
    try:
        import webview
    except ImportError:
        print("界面需要 pywebview，装一下：")
        print("    pip install pywebview pythonnet")
        return 1

    from app.ui.bridge import Api

    page = Path(__file__).resolve().parent / "app" / "ui" / "index.html"
    if not page.is_file():
        print(f"找不到界面文件：{page}")
        return 1

    # 任务栏图标：不设这个的话，Windows 按 pythonw.exe 归组，
    # 任务栏上显示的是 Python 的图标，而且跟别的 Python 程序挤在一起。
    # 设一个自己的 AppUserModelID 才算"一个独立的应用"。
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "PersonalSecretary.Desktop")
    except Exception:
        pass

    icon = Path(__file__).resolve().parent / "assets" / "app.ico"
    webview.create_window("私人秘书", str(page), width=1040, height=680,
                          min_size=(820, 520), js_api=Api(store, plugins, actions))
    # 标题栏图标。pywebview 的 create_window 不收这个参数，只有 start 收。
    webview.start(icon=str(icon) if icon.is_file() else None)
    return 0


def list_plugins(statuses) -> int:
    """`--plugins`：装了哪些、哪个坏了。坏插件不影响主程序，但要说出来。"""
    folder = plugin_dir(Path(__file__).resolve().parent)
    print("插件现状：")
    print(f"  插件目录：{folder}")
    if not statuses:
        print("  一个插件都没装。")
        print(f"  装法：把一个 .py 文件丢进上面这个文件夹；卸载就是删掉它。")
        return 0
    for item in statuses:
        print(("  ✓ " if item.ok else "  ✕ ") + item.describe())
    broken = [item for item in statuses if not item.ok]
    if broken:
        print(f"\n  有 {len(broken)} 个插件没加载起来。"
              "它对主程序没影响，但那个插件自己不会干活。")
        return 1
    return 0


def sync_sources(store, pipeline, sources) -> dict:
    """拉一遍插件来源（课表订阅这类），结果当普通消息走同一条管线。

    一个来源挂了不影响别的：它自己报一句错，其余的照拉 ——
    跟"坏插件不影响启动"是同一条规矩，只是发生在运行期。

    返回 `{"added": 新增条数, "notes": [这次有什么值得告诉用户的]}`。
    **notes 不能省**：像"课表里有单双周但没填学期起点"这种事，
    不带回来的话用户永远不知道有节课排错了，只会觉得这软件算得不准。
    """
    added = 0
    notes: list[str] = []
    for source in sources:
        label = getattr(source, "title", None) or getattr(source, "kind", "?")
        try:
            messages = source.fetch() or []
        except Exception as exc:
            print(f"  ✗ {label} 拉取失败：{type(exc).__name__}: {exc}")
            continue
        notes.extend(list(getattr(source, "notes", None) or []))
        if not messages:
            print(f"  · {label}：这次没有新的")
            continue
        print(f"  · {label}：拿到 {len(messages)} 条")
        for message in messages:
            added += ingest_message(store, pipeline, message)
    return {"added": added, "notes": notes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="私人秘书（开发期命令行）")
    parser.add_argument("text", nargs="*", help="要录入的文字")
    parser.add_argument("--list", nargs="?", const="open", default=None,
                        help="列出待办：open/today/tomorrow/week/next_week/"
                             "overdue/archive/junk")
    parser.add_argument("--done", type=int, help="把某条标记为完成")
    parser.add_argument("--drop", type=int, help="把某条标记为误判（日后调规则的案例）")
    parser.add_argument("--digest", action="store_true", help="生成汇总并打印")
    parser.add_argument("--push", action="store_true", help="生成汇总并真正推送")
    parser.add_argument("--check-push", action="store_true",
                        help="检查推送通道配置（不发消息）")
    parser.add_argument("--schedule", action="store_true",
                        help="启动定时推送（Ctrl+C 停止）")
    parser.add_argument("--next-push", action="store_true",
                        help="看下一次什么时候推、有没有错过的")
    parser.add_argument("--check-qq", action="store_true",
                        help="看 QQ 接入的配置和状态")
    parser.add_argument("--qq", action="store_true", help="启动 QQ 接收")
    parser.add_argument("--deploy", action="store_true",
                        help="一键装 NapCat 并启动（要先给 --account）")
    parser.add_argument("--setup", action="store_true",
                        help="只装不启动")
    parser.add_argument("--account", type=int,
                        help="要登录的 QQ 号（NapCat 按号找配置文件，必须给）")
    parser.add_argument("--dir", help="NapCat 装到哪个目录（默认 data/napcat）")
    parser.add_argument("--ui", action="store_true", help="打开界面窗口")
    parser.add_argument("--shortcut", action="store_true",
                        help="在桌面创建快捷方式")
    parser.add_argument("--file", help="把一个文件（图片/Word/PPT/PDF）变成待办")
    parser.add_argument("--read", help="只看这个文件能提出什么字，不入库")
    parser.add_argument("--stats", action="store_true", help="看统计")
    parser.add_argument("--plugins", action="store_true",
                        help="看装了哪些插件（plugins/ 文件夹里的 .py）")
    parser.add_argument("--sync", action="store_true",
                        help="拉一遍插件来源（课表订阅这类），存成待办")
    parser.add_argument("--selftest", action="store_true", help="检查契约与环境")
    parser.add_argument("--db", help="数据库路径（默认 data/secretary.db）")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    store, source, pipeline, sink, registry = build(args.db)

    if args.plugins:
        return list_plugins(registry.status)

    if args.sync:
        if not registry.sources:
            print("没有可主动拉取的插件来源。")
            print(f"（插件放在 {plugin_dir(Path(__file__).resolve().parent)}，"
                  "课表插件要在 config.toml 的 [timetable] 里填 url）")
            return 0
        result = sync_sources(store, pipeline, registry.sources)
        for note in result["notes"]:
            print(f"  ⚠️ {note}")
        print(f"\n同步完成：新待办 {result['added']} 条")
        return 0

    if args.done:
        store.set_status(args.done, STATUS_DONE, "用户在命令行标记完成")
        print(f"已标完成：#{args.done}")
        return 0

    if args.drop:
        store.set_status(args.drop, STATUS_DROPPED, "用户判定为误判")
        print(f"已标误判：#{args.drop}")
        return 0

    if args.stats:
        for key, value in store.stats().items():
            print(f"{key:>10}: {value}")
        return 0

    if args.check_push:
        print("推送通道现状：")
        for line in channel_status():
            print(f"  · {line}")
        scheduler = Scheduler(store, sink)
        print(f"  · {scheduler.describe()}")
        print(f"  · {mem_describe()}")
        print("\n要真正收到推送，需要在 config.toml 里填 [wecom_bot] webhook，"
              "并把 enabled 改成 true。")
        return 0

    if args.next_push:
        scheduler = Scheduler(store, sink)
        print(scheduler.describe())
        print(mem_describe())
        pending = [slot for slot in due_slots()
                   if not store.already_pushed(slot.batch)]
        if pending:
            print("\n现在就该推的（还没推过）：")
            for slot in pending:
                print(f"  · {slot.batch}「{slot.title}」")
        else:
            print("\n现在没有该推的。")
        recent = store.recent_pushes(limit=5)
        if recent:
            print("\n最近推过：")
            for item in recent:
                mark = "✓" if item["ok"] else "✗"
                print(f"  {mark} {item['batch']}｜{item['channel']}｜{item['message'][:40]}")
        return 0

    if args.schedule:
        return run_scheduler(store, sink)

    if args.check_qq:
        settings = QQSettings.from_config()
        print("QQ 接入现状：")
        print(f"  启用：     {settings.enabled}")
        print(f"  监听：     {settings.bind_host}:{settings.bind_port}{settings.path}")
        print(f"  接收范围： {settings.summary()}")
        print(f"  Token：    {'已设置' if settings.token else '还没设置'}")
        if not settings.group_ids and not settings.receive_all and not settings.inbox_ids:
            print()
            print("  ⚠️ 一个群都没勾，也没有收件箱 —— 现在什么都不会收。")
            print("     在 config.toml 的 [qq] group_ids 里填群号，")
            print("     或者在 inbox_ids 里填小号会话。")
        return 0

    if args.read:
        return read_file(args.read)

    if args.file:
        target = Path(args.file)
        if not target.is_file():
            print(f"文件不存在：{target}")
            return 1
        message = source.from_files([str(target)])
        added = ingest_message(store, pipeline, message)
        print(f"\n录入完成：新待办 {added} 条")
        for task in store.list_tasks("open", limit=5):
            print(f"  #{task.task_id:<4} {task.title}   [{task.due_at or '没写时间'}]")
        return 0

    if args.qq:
        return run_qq(store, pipeline)

    if args.ui:
        return run_ui(store, registry.status, {
            "add_text": lambda text: ingest(store, source, pipeline, text),
            "sync": lambda: sync_sources(store, pipeline, registry.sources),
            "ingest_file": lambda path: ingest_message(
                store, pipeline, explicit_file_message(source, path)),
            "read_file_text": extract_preview,
        })

    if args.shortcut:
        from app.runtime.desktop import create_shortcut

        ok, message = create_shortcut()
        print(("已创建：" if ok else "创建失败：") + message)
        if ok:
            print("注意：快捷方式只打开界面；后台收消息要另外跑 python run.py --qq")
        return 0 if ok else 1

    if args.deploy or args.setup:
        account = args.account
        if not account:
            print("要装哪个号？加一个参数，例如：")
            print("    python run.py --deploy --account 123456789")
            return 1
        token = ensure_token(str(config.get("qq", "token", "") or ""))
        port = int(config.get("qq", "bind_port", 18080) or 18080)
        target = Path(args.dir) if args.dir else deploy_dir()
        if args.setup:
            return run_setup(target, account, token, port)
        if args.dir:
            print("--dir 只在 --setup 时有效；--deploy 用的是默认位置。")
        return run_deploy(store, pipeline, account)

    if args.digest or args.push:
        tasks = store.list_tasks("open")
        payload = build_digest(tasks, title="今天要做的")
        if args.digest:
            # --digest 永远只打印，不真的发出去（免得调试时误发到群里）
            from app.sinks.console import ConsoleSink

            ConsoleSink().deliver(payload)
            return 0
        payload.channel = sink.name
        result = sink.deliver(payload)
        explain = getattr(sink, "explain", None)
        print(explain(result) if explain else f"推送{'成功' if result.ok else '失败'}：{result.message}")
        return 0 if result.ok else 1

    if args.list is not None:
        if args.list == "junk":
            # 废件箱在独立的表里（它是"没被收下的原文"，不是待办）。
            # 留着是为了翻案和调规则 —— CONTRACT.md 第七节第 4 条。
            items = store.list_dropped()
            if not items:
                print("（废件箱是空的）")
                return 0
            print(f"废件箱（{len(items)} 条）—— 这些是没被收下的原文：")
            for item in items:
                origin = f"  ← {item['source_name']}" if item["source_name"] else ""
                print(f"  · {item['text'][:60]}   [{item['reason'][:12]}]{origin}")
            return 0
        tasks = store.list_tasks(args.list)
        if not tasks:
            print(f"（{args.list} 没有内容）")
            return 0
        for task in tasks:
            mark = {"open": "○", "done": "✔", "dropped": "✕"}.get(task.status, "?")
            due = task.due_at or "没写时间"
            origin = f"  ← {task.source_name}" if task.source_name else ""
            print(f"{mark} #{task.task_id:<4} {task.title}   [{due}]{origin}")
        return 0

    text = " ".join(args.text).strip()
    if not text:
        parser.print_help()
        return 0

    added = ingest(store, source, pipeline, text)
    print(f"\n录入完成：新待办 {added} 条（共 {store.count_tasks('open')} 条未完成）")
    store_dump = store.list_tasks("open", limit=5)
    for task in store_dump:
        print(f"  #{task.task_id:<4} {task.title}   [{task.due_at or '没写时间'}]")
    return 0


def _selftest() -> int:
    """第 1 步的门禁：契约完整、配置齐全、目录能建。

    加一个新的必填配置项时，记得往下面的 `required` 里加一行 ——
    否则用户配置漏了会在运行到一半时才炸，而不是启动时就告诉他。
    """
    from app.contracts import models
    from app.contracts.interfaces import (
        DraftEnricher, Sink, Source, Store, TextEnricher,
    )
    from app.runtime.plugins import Registry, plugin_dir
    from app.runtime.plugins import load as load_plugins

    problems: list[str] = []
    for name in ("Message", "TaskDraft", "Task", "PushPayload", "Attachment"):
        if not hasattr(models, name):
            problems.append(f"契约缺数据格式：{name}")
    for iface in (Source, TextEnricher, DraftEnricher, Store, Sink):
        if not getattr(iface, "__abstractmethods__", None):
            problems.append(f"接口没有抽象方法：{iface.__name__}")

    required = {
        "parse": ["filter_strength", "confidence_min", "merge_window_seconds", "due_default_hour"],
        "title": ["max_len", "min_meaningful_chars"],
        "store": ["attachment_retention_days", "keep_dropped_days"],
        "push": ["default_times", "quote_max_chars", "fallback_text_chars"],
        "ai": ["mode", "cloud_priority", "token_budget_per_day"],
        "resource": ["mem_critical_percent", "mem_resume_percent"],
        "risk": ["require_secondary_account"],
    }
    for section, keys in required.items():
        for key in keys:
            if config.get(section, key, None) is None:
                problems.append(f"配置缺项：{section}.{key}")

    if config.load_error:
        problems.append(config.load_error)

    data_dir = default_data_dir()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        problems.append(f"数据目录建不了：{exc}")

    # 插件：坏插件不该拦住启动，但自检必须把它摆出来 —— 否则用户
    # 只会觉得"我装的那个插件怎么好像没反应"。
    root = Path(__file__).resolve().parent
    plugins = load_plugins(root, Registry())
    broken = [item for item in plugins if not item.ok]
    if broken:
        problems.append(f"有 {len(broken)} 个插件加载失败（见下面 ✕ 那几行）")

    print("私人秘书 · 自检")
    print(f"  项目根目录：{config.path.parent if config.path else '?'}")
    print(f"  配置文件：  {config.path}")
    print(f"  数据目录：  {data_dir}")
    print(f"  过滤强度：  {config.get('parse', 'filter_strength')}")
    print(f"  AI 模式：   {config.get('ai', 'mode')}（云端优先={config.get('ai', 'cloud_priority')}）")
    print(f"  插件：      {len(plugins)} 个（{plugin_dir(root)}）")
    for item in plugins:
        print(("    ✓ " if item.ok else "    ✕ ") + item.describe())
    if problems:
        print("\n发现问题：")
        for item in problems:
            print(f"  ✕ {item}")
        return 1
    print("\n✓ 契约完整，配置齐全")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
