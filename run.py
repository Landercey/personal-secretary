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
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

def _running_from_archive() -> bool:
    """是不是"在压缩包里双击"跑起来的（exe 位于 %TEMP% 下面）。

    WinRAR / 7-Zip / Bandizip 双击压缩包里的 exe 时，会先把整包解到临时目录
    再运行 —— 于是程序以为自己"装"在那儿。三个后果都是真实反馈：
      · 配置和待办写进临时目录，关掉（或清理临时文件）就没了；
      · 再解压一份出来，两份程序抢同一个端口 → "端口 18080 被占用"；
      · 杀软对临时目录里的 exe 格外敏感（隔离、拦启动）。
    """
    if not getattr(sys, "frozen", False):
        return False                  # 源码运行（python run.py）不算
    try:
        exe = Path(sys.executable).resolve()
        temp = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return False
    return temp == exe.parent or temp in exe.parents


def _make_output_safe() -> None:
    """让 stdout/stderr 变成"打印永远不会抛异常"。

    ⚠️ 必须在**两个**地方调，少一个都会踩坑（2026-10-16 用户报的那次）：
      · 模块加载时调一次 —— 命令行那条路（`--serve` 被重定向到文件）默认按
        系统代码页 GBK 编码，一句带 `✓/✗/⚠️` 的提示就抛 UnicodeEncodeError；
      · `import webview` **之后**再调一次 —— pywebview 的 http.py 顶部发现
        stdout 是 None（窗口模式下就是这样）就自己塞一个
        `open(os.devnull, "w")` 进去，那个文件对象是 GBK + errors="strict"，
        把我们上面设好的 utf-8 顶掉了。

    后果不是"少显示一行"，而是**把整个动作带崩**：用户看到的是
    "sync_now 出错：'gbk' codec can't encode character '\\u2717'" ——
    课表拉取失败本来要报一句原因，报错那句话自己先崩了，真正的原因反而没了。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass                      # 窗口模式下这两个是 None，跳过就行


_make_output_safe()

from app.contracts.models import STATUS_DONE, STATUS_DROPPED     # noqa: E402
from app.pipeline.chain import Pipeline                          # noqa: E402
from app.pipeline.chat_filter import ChatNoiseEnricher           # noqa: E402
from app.pipeline.file_enricher import FileEnricher              # noqa: E402
from app.pipeline.parse_enricher import RuleParseEnricher        # noqa: E402
from app.runtime.config import config, default_data_dir, default_root  # noqa: E402
from app.runtime import heartbeat                                # noqa: E402
from app.runtime import plugins as plugin_module                 # noqa: E402
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

# 程序自己的目录。源码运行 = 这个文件夹；打包成 exe = exe 旁边那个文件夹。
ROOT = default_root()
# 随包发的**只读**文件（界面 html、schema.sql、脚本、自带插件）在哪。
# 源码运行 = 项目根目录；打包后 = PyInstaller 解出来的 _internal。
RESOURCE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

# 掉线自救最多重启几次。为什么要上限（2026-10-16 加的这道闸）：
# 它会**带号重新登录**，反复来既是无用功，又更像"异常登录"。
# 用完就停手，改成在界面上和日志里告诉用户去手动点。
AUTO_RESTART_MAX = 3

# 启动自检发现的问题（库坏了/盘满了/安全模式），界面读它当提示条。
# 用列表原地改（`STARTUP_NOTES[:] = ...`）而不是重新赋值：别的模块 import 的是同一个对象。
STARTUP_NOTES: list[dict] = []


def ensure_bundled_files() -> int:
    """第一次运行时，把随包发的插件补到用户的 plugins/ 里。返回补了几个。

    只补缺不覆盖（见 plugins.seed）。源码运行时 RESOURCE 和 ROOT 是同一个地方，
    本来就在，这个函数什么也不会做。
    """
    if RESOURCE == ROOT:
        return 0
    return plugin_module.seed(ROOT, RESOURCE / "plugins")


# 少了就彻底用不了的那几个文件（打包后在 _internal 里）。
# 杀软误报最常见的下场就是其中一个被吃掉 —— 那时程序连界面都画不出来。
CRITICAL_FILES = ("app/ui/index.html", "app/store/schema.sql",
                  "app/pipeline/extract/win_ocr.ps1")


def missing_app_files() -> list[str]:
    """程序自己的关键文件缺了哪几个（缺就是被杀软删了，或者解压不完整）。"""
    return [name for name in CRITICAL_FILES if not (RESOURCE / name).is_file()]


def alert_missing_files(missing: list[str]) -> None:
    """把"文件没了"这件事**弹到屏幕上**说清楚，别让它变成"双击没反应"。

    用户报过的原话："测试时被 Windows 安全中心拉进隔离了"——
    隔离之后双击 exe 什么都不发生，他只会以为软件坏了。
    """
    from app.runtime.desktop import alert

    alert("私人秘书 · 文件不见了",
          "程序自己的文件少了这几个：\n  " + "\n  ".join(missing) +
          "\n\n最可能的原因：**被杀毒软件（Windows 安全中心）当成病毒隔离了**。\n"
          "这个程序是打包出来的，杀软对这类没签名的程序经常误报。\n\n"
          "怎么办：\n"
          "1. 打开 Windows 安全中心 → 病毒和威胁防护 → 「保护历史记录」，\n"
          "   里面能找到它删掉的东西，选「还原」；\n"
          "2. 再把整个软件文件夹加进「排除项」（病毒和威胁防护 → 管理设置 →\n"
          "   → 排除项 → 添加文件夹）；\n"
          "3. 还不行的话，用最新的补丁包重新解压覆盖一遍（配置和待办不会丢）。\n\n"
          f"程序目录：{ROOT}")


def build(database: str | Path | None = None, safe_mode: bool = False):
    """把零件装配起来。换数据库路径主要是为了测试。

    加一个新零件/插件的标准动作就是改这里（下面四行）：
        store / source / pipeline / sink
    比如以后接 QQ 来源，就是在 source 那边多一个 QQSource；
    加 AI 理解，就是往 plugins/ 丢一个插件（不用改这个文件）。

    插件的加载放在装配的最后一步：**坏插件只会被记一笔**，
    不影响这里把主程序装配起来（CONTRACT.md 第二节硬性约束）。
    返回值里多带的 registry 就是给 --plugins / --sync / 界面 / 自检看的。
    """
    db_path = Path(database) if database else default_data_dir() / "secretary.db"
    store = SqliteStore(open_database(db_path))
    source = ManualSource()

    registry = Registry()
    if not safe_mode:
        load_plugins(ROOT, registry)     # 加载结果记在 registry.status 上
    # 安全模式：一个插件都不加载。它只在"连续几次没起来"时自动触发，
    # 目的是让用户至少能进界面把配置改回来。

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


# 先发文件、后跟一句话的那条路：文件先"攥着"，等下一条字建出待办再挂上去。
# 只放内存里：窗口就 60 秒，进程重启了也就这一次丢了（文件还在 messages 里）。
PENDING_FILES: dict[tuple[str, str, str], tuple[float, "Message"]] = {}


def _pending_key(message) -> tuple[str, str, str]:
    return (str(message.source_kind or ""), str(message.source_name or ""),
            str(message.sender or ""))


def _remember_pending_files(message, window: int) -> None:
    if window <= 0 or not message.attachments:
        return
    PENDING_FILES[_pending_key(message)] = (time.time(), message)


def _take_pending_files(message, window: int):
    """刚才有没有一条"只带文件、说不出事"的消息在等这条字。"""
    if window <= 0:
        return None
    held = PENDING_FILES.pop(_pending_key(message), None)
    if held is None:
        return None
    stamp, older = held
    return older if time.time() - stamp <= window else None


def _sweep_pending_files(store, window: int, force: bool = False) -> None:
    """攥过期的（或进程要退了）：记进废件箱，让用户还能一键捡回来。"""
    now = time.time()
    for key, (stamp, held) in list(PENDING_FILES.items()):
        if not force and now - stamp <= max(window, 0):
            continue
        PENDING_FILES.pop(key, None)
        names = "、".join(a.name for a in held.attachments if getattr(a, "name", ""))
        try:
            # 这条消息可能已经因为"文字不像待办"进过废件箱了 ——
            # 那就别再记一笔，同一个文件在废件箱里出现两遍只会更难找。
            if store.has_dropped(held.msg_id):
                continue
            store.log_dropped_text(
                f"[文件] {names}" + (f"\n{held.text[:120]}" if held.text else ""),
                "文件：内容没被认成待办（点「这条其实有用」能连文件一起捡回来）",
                held.source_kind, held.source_name, held.msg_id)
        except Exception:
            pass


def ingest_message(store, pipeline, message, refiner=None) -> int:
    """一条标准消息 → 待办。返回新入库的条数。

    整条流水线一共五步（QQ 和手动录入走的是同一条）：
      1. 存原始消息（留证据，出问题能回查"这条到底收没收到"）
      2. 理解层跑一遍：消息 → 草稿
      3. 草稿入库（重复的会被自动挡掉）
      4. 被判为闲聊/碎片的，记进废件箱（不推送，但留证据）
      5. 顺手清掉过期记录

    `refiner`：群消息走的那条路会把 AI 插件**跳过**（`skip={"ai"}`），
    规则版先入库让用户马上看到；AI 交给后台线程回来精修（见 runtime/ai_refine.py）。
    手动录入/拖文件不传它 —— 那是用户正等着结果的动作，同步跑完更直观。
    """
    # 同一条消息只走一次管线。
    # 断线重连、每次启动的「补收」都会把最近的历史再喂一遍 —— 以前每条都
    # 重新跑一遍理解层，于是同一条待办被记一次"合并重复"、同一条闲聊被记一次
    # "拦下"：今天实测 6 条待办被记了 104 次重复。消息表按 msg_id 去重，
    # 见过就直接跳过（代价：万一上一次存完消息就崩了，这条不会自动重跑，
    # 用 `--replay 消息编号` 能手动重来）。
    if store.has_message(message.msg_id):
        return 0
    store.save_message(message)
    prepared, drafts = pipeline.run(message, skip={"ai"} if refiner else ())
    # 再存一次。第一次存的是"还没读附件"的原样消息，跑完管线才有正文 ——
    # 只存第一次的话，文件类消息在库里永远是空的，
    # "这条为什么没收下"就查不出来了。save_message 是 INSERT OR REPLACE，
    # 重复调用没副作用。
    store.save_message(prepared)
    stored = store.save_drafts(prepared, drafts)
    if refiner is not None:
        # 交给后台：问模型 → 回来核对"这条还没被用户动过" → 只改标题/时间/地点。
        # 不 await、不 wait —— 收消息那条线程一秒都不等它。
        refiner.submit(prepared, stored)

    window = int(config.get("parse", "merge_window_seconds", 60) or 0)

    # 合并窗口，两个方向都要管（用户 2026-10-17 报的："字和文件会分开发"）：
    #
    #   ① 字先发、文件后到：文件自己解析不出事 → 挂到刚才那条上（下面这段）。
    #   ② 文件先发、字后到：文件先被"攥着"，等后面那条字建出待办再挂上去。
    #      以前只做了 ①，文件先发的顺序就**整份丢了**（只留在消息表里）。
    if stored and window > 0:
        held = _take_pending_files(prepared, window)
        if held is not None:
            attached = store.attach_message_files(stored[0].task_id, held)
            print(f"  ↳ 把刚才那份附件挂到「{stored[0].title}」（{attached} 个）")

    if not drafts and prepared.attachments:
        recent = store.recent_task_for(prepared.source_kind, prepared.source_name,
                                       prepared.sender, window)
        if recent is not None:
            attached = store.attach_message_files(recent.task_id, prepared)
            print(f"  ↳ 合并到刚才那条「{recent.title}」（挂了 {attached} 个附件）")
            store.purge_old_dropped()
            store.purge_old_attachments()
            return 0
        # 没有"刚才那条"可挂：先攥着，等后面那句"这个明天要交"。
        _remember_pending_files(prepared, window)

    # 攥太久还没人来认领的：记进废件箱（点「这条其实有用」能连文件一起捡回来），
    # 别让一份附件只在磁盘上躺着、界面上哪儿都找不到。
    _sweep_pending_files(store, window)

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
    # 顺手把过期附件（图片/文件）清掉：这个开关以前只写在文档里，
    # 代码从来没读过，结果是"承诺 30 天清理，实际永远不清理"。
    store.purge_old_attachments()
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


def ingest_file(store, source, pipeline, path: str) -> dict:
    """拖进来的文件走这条：除了"建了几条"，还要把**为什么没建**带回去。

    只回一个数字的话，用户看到的就是"我把 PPT 拖进去了，结果什么都没读取"——
    他分不清是文件里没字、格式不支持、还是内容不像待办（那样会进废件箱）。
    理解层其实已经算出了原因（`message.raw["extract_notes"]`），
    以前只是没人往界面上带。
    """
    message = explicit_file_message(source, path)
    added = ingest_message(store, pipeline, message)
    return {"added": added,
            "notes": [str(item) for item in (message.raw.get("extract_notes") or [])]}


def promote_dropped(store, pipeline, row: dict, paths: list[str]) -> int:
    """废件箱里的一条 → 待办（用户在界面上按了「这条其实有用」）。

    为什么不复用 `ingest_message`：那里有个"合并窗口"—— 同一来源同一个发送人
    60 秒内的消息会挂到**上一条**待办上。用户在这儿是**指着这一条**要它变成
    待办，被塞进一条不相干的待办里等于按了没反应。所以这里只跑理解层，
    然后直接入库。

    正文优先用 messages 表里的**全文**：废件箱那笔为了不占地方截到 500 字。
    """
    from app.contracts.models import SOURCE_MANUAL, Message, TaskDraft
    from app.pipeline.actionable import strip_placeholders
    from app.sources.manual import build_attachment

    original = store.get_message(str(row.get("msg_id") or "")) or {}
    msg_id = str(row.get("msg_id") or "") or f"drop:{row.get('drop_id') or 0}"
    text = str(original.get("text") or row.get("text") or "").strip()
    source_kind = str(row.get("source_kind") or SOURCE_MANUAL)
    source_name = str(row.get("source_name") or "")
    sender = str(original.get("sender") or "")
    attachments = [build_attachment(path) for path in paths]

    message = Message(
        msg_id=msg_id, source_kind=source_kind, source_name=source_name,
        sender=sender, sent_at=str(original.get("sent_at") or ""),
        text=text, attachments=attachments,
        # explicit：用户已经点着按钮表过态了，理解层不该再拿规则替他判断。
        raw={"explicit": True, "promoted_from_drop": True},
    )
    _prepared, drafts = pipeline.run(message)
    stored = store.save_drafts(message, drafts)

    if not stored and attachments:
        # 一张图、图里一个字都没有（群里那种表情包）。理解层对"群里没字的图"
        # 是**刻意不收**的；但用户亲手按了这个按钮，就是"这张我要留着"，
        # 那就按附件收下 —— 否则按钮等于没反应，比不收更让人难受。
        stored = store.save_drafts(message, [TaskDraft(
            title=strip_placeholders(text).strip() or f"来自{source_name or '群里'}的图片",
            detail=text, source_kind=source_kind, source_name=source_name,
            sender=sender, origin_msg_id=msg_id, attachments=attachments,
            quoted=text,
        )])
    return len(stored)


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


def replay_message(store, pipeline, msg_id: str) -> int:
    """把库里某条消息重新跑一遍理解层，看现在会解析成什么。**不入库、不推送**。

    为什么要有：用户说"这条为什么没收下/收错了"，光看日志只能看到结论；
    拿原话重跑一遍，才能看见规则每一步的判断（也方便修完规则后验证）。
    """
    from app.contracts.models import Message

    row = store.get_message(str(msg_id or "").strip())
    if not row:
        print(f"库里没有这条消息：{msg_id}")
        print("提示：消息编号在「废件箱 → 详情」或反馈包里能看到（形如 qq:123456）。")
        return 1
    message = Message(
        msg_id=str(row.get("msg_id") or ""),
        source_kind=str(row.get("source_kind") or "manual"),
        source_name=str(row.get("source_name") or ""),
        sender=str(row.get("sender") or ""),
        sent_at=str(row.get("sent_at") or ""),
        text=str(row.get("text") or ""),
        raw=dict(row.get("raw") or {}))
    prepared, drafts = pipeline.run(message)
    print(f"消息：{message.source_name}｜{message.sent_at}｜{message.sender}")
    print(f"正文（前 200 字）：{message.text[:200]!r}")
    skip = prepared.raw.get("skip_reason") or ""
    print(f"闲聊过滤：{'拦下 —— ' + skip if skip else '放行'}")
    for line in prepared.raw.get("dropped_lines", [])[:10]:
        print(f"  丢弃的行：{line[:60]!r}")
    for err in prepared.raw.get("enricher_errors", []):
        print(f"  插件出错：{err}")
    print(f"解析结果：{len(drafts)} 条")
    for draft in drafts:
        print(f"  · {draft.title!r}｜截止 {draft.due_at}｜地点 {draft.location}")
    if not drafts:
        print("（一条都没出：上面那几行说明了是哪一步拦下的）")
    return 0


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


def start_refiner(store, pipeline):
    """起一个 AI 精修线程（没有 AI 插件就返回 None）。

    接收端只跑规则，AI 在后台补 —— 见 app/runtime/ai_refine.py 的说明。
    """
    enricher = next((e for e in pipeline.draft_enrichers
                     if getattr(e, "name", "") == "ai"), None)
    if enricher is None:
        return None
    from app.runtime.ai_refine import Refiner

    return Refiner(store, enricher,
                   on_event=lambda text: print(f"  [AI] {text}")).start()


def run_qq(store, pipeline) -> int:
    """启动 QQ 接收，一直跑到用户按 Ctrl+C。

    为什么放在这里而不是 app 里：起服务、接线、打印状态是"装配"的活，
    全项目只有 run.py 负责装配。
    """
    settings = QQSettings.from_config()
    heartbeat.clear_stop()      # 清掉上次留下的停止请求，不然一起来就退出
    if not heartbeat.claim_single_instance():
        print("⚠️ 已经有一个后台在跑了（同一个数据目录），这个就不重复起了。")
        return 1
    # 记下"我是什么时候起来的、当时要重启那几项是什么值" ——
    # 界面靠它判断"改了设置到底要不要重启"（见 heartbeat.restart_needed）。
    heartbeat.mark_start(store)
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

    refiner = start_refiner(store, pipeline)

    def on_message(message) -> None:
        added = ingest_message(store, pipeline, message, refiner=refiner)
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
    # 接收端起来了 = 能用了（`--serve` / `--qq` 都要清启动计数，见 run_deploy 里的说明）
    from app.runtime import startup

    startup.mark_ready()

    print("=" * 62)
    print("  私人秘书 · QQ 接收")
    print("=" * 62)
    print(f"  监听：       {settings.bind_host}:{settings.bind_port}{settings.path}")
    print(f"  接收范围：   {settings.summary()}")
    # ⚠️ token 打码再打印：这段输出会进 data/background.log，而用户经常把日志
    # 整段贴到群里求助（真实发生过）。NapCat 那边程序会自动写好，用不着抄。
    print(f"  NapCat 需要填的地址（token 已打码）：")
    print(f"      ws://{settings.bind_host}:{settings.bind_port}{settings.path}"
          f"?access_token={'***' if settings.token else '（没设）'}")
    print("      （完整地址程序会自动写进 NapCat 的配置；要手动填，"
          "在 config.toml 的 [qq] token 里看）")
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
            # 每 10 秒往库里记一笔"我还活着"（heartbeat 自己节流）。
            # 界面靠它区分"你没配 QQ"和"配了但进程没起来" ——
            # 没有这个，界面左下角那个"11 个群"会一直显示得一切正常。
            heartbeat.beat(store, (
                f"登录号 {state.get('self_id') or '?'}｜收到 {state.get('received', 0)}"
                if state.get("connected") else
                # 绑不上端口时界面要看得见真正的原因，不能只说"等连上来" ——
                # 新用户反馈里那句"后台：在跑（等扫码登录）"就是这么误导人的。
                (state.get("error") or "等 NapCat 连上来")),
                # 顺手把"加过哪些群"捎给界面 —— 它开不了跟 QQ 说话的口子，
                # 只能这么把列表带过去（设置页要拿它做勾选）。
                groups=source.groups())
            if heartbeat.stop_requested():
                print("  收到停止请求（界面里点的「停止后台」），收工。")
                break
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
        if refiner is not None:
            refiner.stop()
        # 还没等到"那句字"的附件记进废件箱，别让它只在磁盘上躺着
        _sweep_pending_files(store, 0, force=True)
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
    from app.runtime import deploy

    return deploy.default_dir()


def background_prelude() -> None:
    """后台进程启动的第一步：清掉上一次留下的停止请求。

    ⚠️ 不清的话出**真事故**（2026-10-10 用户报的）：上回点过「停止后台」，
    那个文件一直躺在 data/ 里；这次启动后台，run_deploy 等扫码的循环每次
    开头都看它一眼 —— 结果是**登录成功之后两秒就自己退出**，顺手把 NapCat
    也杀了。用户看到的是"扫上码了、左下角也显示了，然后突然退了"。

    （原来只有命令行那条路 `--qq` 清了，界面按钮走的 `--serve` 没清。）
    """
    heartbeat.clear_stop()
    # 输出被重定向到文件时，Python 默认是**块缓冲** —— 后台明明在跑，
    # 界面里那块「上次启动的输出」却一直是空的（2026-10-13 用户登不上，
    # 那份日志 0 字节，最后只能去翻 NapCat 自己的日志）。
    # 改成按行刷：出问题时至少有东西看。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass            # 冻结成 exe 且没有 stdout 时就这样，不是致命问题


def run_setup(onekey_dir: Path, account: int, token: str, port: int,
              notify=None) -> int:
    """只装不启动：下包 → 跑官方安装器 → 写我们的配置。

    `notify` 是"报一句进展"的钩子：默认打日志 + 写进度文件给界面看；
    从界面启动时（run_deploy）还会顺手报个心跳 —— 装的过程中界面
    必须知道后台在忙，否则用户会以为死了，再点一次「启动 QQ」。
    """
    from app.runtime import deploy

    # 装的过程要几分钟，而界面是**另一个进程** —— 所以每报一句都顺手写进
    # deploy.progress.json，界面那边每 5 秒读一次，用户就能看见"到哪一步了"。
    if notify is None:
        def notify(text: str, percent: int | None = None) -> None:   # noqa: F811
            print(text)
            deploy.report(text, percent=percent)

    onekey_dir.mkdir(parents=True, exist_ok=True)
    if deploy.is_installed(onekey_dir):
        print("已经装过了，跳过下载安装。")
    else:
        # 这张包是用户自己放进来的（别人发他的离线包）还是我们下的？
        # 他自己那份装完不删 —— 删了下次出问题又要重发一遍。
        provided = deploy.local_onekey(onekey_dir) is not None
        try:
            zip_path = deploy.download_onekey(onekey_dir, notify)
            deploy.extract(zip_path, onekey_dir, notify)
            deploy.run_installer(onekey_dir, notify)
        except Exception as exc:
            print(f"\n安装失败：{exc}")
            deploy.report(f"安装失败：{exc}", active=False)
            return 1
        deploy.report("装好了，准备启动", active=False)
        # 装成功了就把安装包删掉 —— 那是个 300 MB 的 zip，留着纯占地方。
        # 用户要的是"全在一个文件夹里、删了就干净"，这个也算在里面。
        # 装失败时**不删**：留着重试就不用再下一次。
        # ⚠️ 离线包（他自己放进来那份）不删。
        if not provided:
            try:
                zip_path.unlink()
            except OSError:
                pass

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
        def tick(text: str, percent: int | None = None) -> None:
            print(text)
            deploy.report(text, percent=percent)
            # 一边装着一边报心跳：不然界面认为"后台没在跑"，
            # 用户再点一次「启动 QQ」就会**第二个进程在同一份目录上再装一遍**。
            heartbeat.beat(store, "正在安装 NapCat", force=True)

        code = run_setup(onekey_dir, account, token, port, notify=tick)
        if code != 0:
            return code

    shell = deploy.find_shell_dir(onekey_dir)
    assert shell is not None
    # 先把上次没退干净的收掉，再起新的。
    # 启动器把 QQ 拉起来之后自己会退出，所以"停止后台"经常清不掉那一窝 QQ；
    # 它们留在后台抢同一个号的登录，表现就是"启动很久没反应/扫了码还是登不上"。
    strays = deploy.stray_processes(shell)
    if strays:
        # 界面上得动起来：这一步要几百毫秒到几秒，什么都不显示又会被当成卡死。
        deploy.report(f"正在清理上次残留的 QQ 进程（{len(strays)} 个）…", active=True)
        deploy.stop_all(shell)
        print(f"  清掉了 {len(strays)} 个上次残留的 QQ/NapCat 进程")
    deploy.write_onebot_config(shell, account, token, port)

    # ① 先起我们的接收端
    settings = QQSettings.from_config()
    if not settings.enabled:
        print("QQ 接入没启用（config.toml 的 [qq] enabled）。")
        return 1
    refiner = start_refiner(store, pipeline)
    source = QQSource(settings=settings,
                      on_message=lambda m: ingest_message(store, pipeline, m,
                                                          refiner=refiner),
                      on_status=lambda s: s.get("error") and print(f"  ⚠️ {s['error']}"))
    if not source.start():
        print("接收端没起来，先别启动 NapCat。")
        return 1
    # 端口是**异步**绑的：等它一下。绑不上就别去起 NapCat ——
    # 它连不进来，只会干等五分钟、然后被"掉线自救"反复带号重启
    # （新用户反馈里那串"端口 18080 被占用"+"自动带号重启（第 1/3 次）"就是这个）。
    for _ in range(15):                       # 最多等 3 秒，正常情况下第一轮就过
        if source.status().get("running") or source.port_busy():
            break
        time.sleep(0.2)
    if source.port_busy():
        print(f"  ⚠️ {source.status().get('error')}")
        print("     关掉那一份之后再点「启动 QQ」就行（这一下没起 NapCat，不用担心）。")
        deploy.report("端口被占用：关掉另一份程序再点启动", active=False)
        return 1
    print(f"  接收端已监听 127.0.0.1:{port}")
    # 到这儿算"能用了"：清启动计数。⚠️ `--serve` 也必须清 ——
    # 只在界面那条路清的话，每开一次（后台+界面各加一次）就攒 1，
    # 三次之后正常启动也会被误判成"打不开"进安全模式（2026-10-17 真踩到）。
    from app.runtime import startup

    startup.mark_ready()

    # ② 再起 NapCat
    #    窗口藏起来：不藏的话用户点一下「启动 QQ」就蹦出两个终端，
    #    十有八九会以为是病毒弹的（二维码我们自己在界面里显示，不需要它那个窗）。
    #    它的输出照样留着（写 napcat.log）—— 窗口藏起来之后空句柄会让它报错退出。
    #    **带上登录号** = 让 NapCat 快速登录：本机存着的那次登录态直接用，
    #    不用再扫码。以前没带这个参数，于是每次重启后台/关机再开/更新完
    #    都要用户重扫一遍（2026-10-13 报的"频繁登录太麻烦"就是这个）。
    napcat = deploy.start_shell(shell, account)
    print(f"  NapCat 已启动（PID {napcat.pid}），它的输出在 "
          f"{deploy.default_dir() / 'napcat.log'}")
    deploy.report("NapCat 已启动，等扫码登录", active=False)

    print()
    print("=" * 62)
    print(f"  先按登录号 {account} 自动登录（上一次扫过码就不用再扫了）")
    print("  自动登录没成的话会自动退回扫码，二维码在这里：")
    print(f"      {deploy.qr_path(shell)}")
    print("  ⚠️ 二维码约 2 分钟过期，过期了点界面上的「重新出码」")
    print("=" * 62)

    last_line = ""
    repaired = 0
    qr_fallback = False
    last_port_hint = ""          # "端口被占着"这句只打一次，别每次循环都刷
    # 掉线自救用的两个状态：从什么时候开始连不上、已经自动重启过几次
    offline_since: float | None = None
    auto_restarts = 0
    try:
        while True:
            time.sleep(2)
            state = source.status()
            heartbeat.beat(store, (
                f"登录号 {state.get('self_id') or '?'}｜收到 {state.get('received', 0)}"
                if state.get("connected") else "等扫码登录"),
                # ⚠️ 这一处才是**真正在跑**的那个循环（run_serve 里另建了一个
                # QQSource，见上面那个同名变量）—— "加过哪些群"必须从这里捎出去。
                # 2026-10-13 踩过：只在等扫码那个循环里加了，界面读到的永远是空的。
                groups=source.groups())
            if heartbeat.stop_requested():
                print("  收到停止请求，收工。")
                break

            # ③ 盯着我们写的那份 OneBot 配置。
            #    首次登录时 NapCat 会用登录号重建一份**空白**配置，把我们写好的覆盖掉 ——
            #    它那份没指向我们，于是永远连不上，用户只能再点一次「启动 QQ」。
            #    这里自动补好并重启一次（此时 QQ 已经登录，重启不用再扫码）。
            if (not state.get("connected") and repaired < 2
                    and not deploy.onebot_config_ok(shell, account, token, port)):
                repaired += 1
                print(f"  ⚠️ NapCat 自己重建了配置（首次登录就会这样），"
                      f"已重新写好并重启它（第 {repaired} 次）")
                deploy.write_onebot_config(shell, account, token, port)
                deploy.report("NapCat 重建了配置，已自动补好并重启一次", active=False)
                if napcat.poll() is None:
                    deploy.stop_shell(napcat, shell)   # 连子进程一起收（否则每修一次漏一窝 QQ）
                napcat = deploy.start_shell(shell, account)
                time.sleep(3)                  # 让它先起来，别立刻又判一次
                continue

            # ④ 快速登录没成（那个号正登在电脑上的 QQ 里之类的）→ 退回扫码。
            #    不退的话用户对着一个死掉的进程干等，界面上什么都不说。
            if (not qr_fallback and deploy.fast_login_failed(napcat, state.get("connected"))):
                qr_fallback = True
                why = deploy.napcat_error_hint() or "自动登录没成"
                print(f"  ⚠️ {why}\n     改用扫码登录，二维码：{deploy.qr_path(shell)}")
                deploy.report(f"自动登录没成（{why[:40]}），改用扫码", active=False)
                deploy.stop_shell(napcat, shell)
                napcat = deploy.start_shell(shell)      # 不带号 = 出二维码
                time.sleep(3)
                continue

            # ⑤ 掉线自救（2026-10-16 用户报："早上还要点一次重新启动 QQ 才行"）。
            #    06:30 网络抖了一下 → 登录态掉了 → NapCat 退回要二维码、每 2 分钟报
            #    `Login Error, ErrType 1 ErrCode 3`，**但进程一直活着** ——
            #    所以第 ④ 条那个判据（进程已退出）永远不成立，程序只能干等。
            #    有效的动作只有一个：带号重启（= 用户手点「重新启动 QQ」那下，
            #    它会拿本机存的登录态再登一次，不用扫码）。次数有上限，见那条注释。
            if state.get("connected"):
                offline_since = None
                auto_restarts = 0            # 连上过就重新给满额
            else:
                # 端口被别人占着 = 收消息的"耳朵"根本没开，跟登录态一点关系都没有。
                # 这时候去重启 NapCat 只会白白多登几次（QQ 会风控），而且永远好不了。
                # 新用户反馈里那一串"端口 18080 被占用"后面跟着"自动带号重启（第 1/3 次）"
                # 就是这么来的。
                if source.port_busy():
                    if last_port_hint != "busy":
                        last_port_hint = "busy"
                        print("  ⚠️ 收消息的端口被占着，先不自动重启 QQ ——"
                              "关掉另一份程序（或上次没退干净的进程）就会自己接上。")
                        deploy.report("端口被占用：关掉另一份程序就会自己接上",
                                      active=False)
                    offline_since = time.time()      # 别让"没连上"的计时继续攒
                    time.sleep(2)
                    continue
                last_port_hint = ""
                if offline_since is None:
                    offline_since = time.time()
                minutes = int(config.get("qq", "auto_restart_minutes", 5) or 0)
                cap = AUTO_RESTART_MAX
                # 每日总闸：连上就重新给满额的话，一天重启 5 次后台就是 15 次登录。
                # QQ 侧看到的是"同一个号一天里反复登录" —— 那才容易被盯上。
                from app.runtime import restart_budget

                day_cap = int(config.get("qq", "restart_cap_per_day", 6) or 0)
                # 夜里不自动重连：半夜没人看推送，多登一次只是多一分被风控的形状。
                if restart_budget.blocked_by_quiet():
                    if last_port_hint != "quiet":
                        last_port_hint = "quiet"
                        print("  · 现在是免打扰时段，先不自动重连 QQ ——"
                              "天亮了会自己接上（想现在接就点「启动 QQ」）。")
                        deploy.report("夜里先不自动重连，早上会自己接上", active=False)
                    offline_since = time.time()
                    time.sleep(2)
                    continue
                if not restart_budget.allow(store, day_cap):
                    if last_port_hint != "budget":
                        last_port_hint = "budget"
                        used_today = restart_budget.used(store)
                        print(f"  ⚠️ 今天已经自动重连 {used_today} 次（上限 {day_cap}），"
                              "先不自动重启了 —— 去看一眼手机上 QQ 有没有提示。")
                        deploy.report("今天自动重连次数用完，等你自己确认", active=False)
                    offline_since = time.time()
                    time.sleep(2)
                    continue
                last_port_hint = ""
                if deploy.should_restart_for_login(
                        time.time() - offline_since, minutes=minutes,
                        used=auto_restarts, cap=cap,
                        proc_alive=napcat is not None and napcat.poll() is None,
                        connected=False):
                    auto_restarts += 1
                    restart_budget.note(store)
                    print(f"  ⚠️ 已经 {minutes} 分钟没连上（NapCat 还活着，多半是掉登录态了），"
                          f"自动带号重启一次重试快速登录"
                          f"（第 {auto_restarts}/{cap} 次）")
                    deploy.report("掉线了，正在自动重连", active=False)
                    deploy.stop_shell(napcat, shell)
                    napcat = deploy.start_shell(shell, account)
                    offline_since = time.time()      # 重新开始计时，别连环重启
                    time.sleep(3)
                    continue
                if auto_restarts >= cap and time.time() - offline_since > 30 * 60:
                    # 自动那几下都用完了：把话说清楚，让用户自己接手
                    # （半小时提一次就够，别把日志刷满）
                    print("  ⚠️ 自动重连试完了还是没连上 —— 在「📱 QQ 接收」里点"
                          "「重新启动 QQ」，或者扫一次码。")
                    offline_since = time.time()      # 一小时后再提醒一次就行

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
        if refiner is not None:
            refiner.stop()
        _sweep_pending_files(store, 0, force=True)
        # 连子进程一起收：真正的 QQ 是 NapCat 的子进程，光 terminate 启动器
        # 会留下一堆 QQ 在跑（占内存 + 锁着 napcat/ 目录）—— 见 deploy.stop_shell。
        if napcat is not None:
            deploy.stop_shell(napcat, shell)
    return 0


def run_serve(store, pipeline, sink, account: int | None = None) -> int:
    """后台全开：定时推送 + QQ 接收（需要的话先装好 NapCat）。**一个进程。**

    为什么合成一个：
      · 开机自启只要挂一条，不用挂两条；
      · 少一个进程 = 少一份内存、少一个崩溃点（旧项目就是两个进程）；
      · 界面上那个「启动 QQ」按钮起的也是它。
    定时推送本来就跑在自己的线程里（`Scheduler.start()`），塞进来很自然。
    """
    background_prelude()
    # 只允许一个后台：不然开机自启那份 + 手动点的那份会各推各的（用户报过）。
    if not heartbeat.claim_single_instance():
        print("⚠️ 已经有一个后台在跑了（同一个数据目录），这个就不重复起了。")
        print("   它可能是开机自启拉起来的。要换成这个，先在界面上点「停止后台」。")
        return 1
    # 同上：界面拿这份记录判断"设置改完要不要重启"
    heartbeat.mark_start(store)
    scheduler = None
    if config.get("push", "enabled", True) and parse_times_or_empty():
        scheduler = Scheduler(store, sink, on_event=lambda text: print(f"  [推送] {text}"))
        try:
            for batch in scheduler.run_once():
                print(f"  [补推] {batch}")
            scheduler.start()
            print(f"  定时推送：{scheduler.describe()}")
        except Exception as exc:                      # 推送起不来不该拖累收消息
            print(f"  ⚠️ 定时推送没起来（{type(exc).__name__}: {exc}），收消息照常")
            scheduler = None
    else:
        print("  定时推送没开（config.toml 的 [push] enabled / times）")
    try:
        if account:
            return run_deploy(store, pipeline, account)
        # 走到这儿说明没给登录号。**不能就这么去等连接** ——
        # 没有号就不会去装/起 NapCat，而接收端会一直等一个永远不来的连接，
        # 用户看到的就是"卡住"。说清楚，别让他猜。
        print("⚠️ 没填要登录的 QQ 号（config.toml 的 [qq] account）。")
        print("   没有号就没法自动起 NapCat，现在只起接收端，")
        print("   等你自己开 NapCat 连过来。要自动装+起，请填上那个号。")
        return run_qq(store, pipeline)
    finally:
        if scheduler is not None:
            scheduler.stop()


def open_ui(store, source, pipeline, sink, registry) -> int:
    """打开界面窗口。

    ⚠️ 界面和"收消息"是**两个独立进程**，共用同一个 SQLite 库。
    好处：界面关掉不影响后台收消息；后台也不用为了显示而常驻一个窗口。
    这也解释了为什么值得用网页界面 —— 那 400 多 MB 只在窗口开着时占。

    界面要用的"理解层动作"（手动录入、拉课表）在这里拼好再递进去，
    而不是让界面自己去 import 那些模块 —— 全项目只有这里能装配。
    """
    try:
        import webview
    except ImportError:
        print("界面需要 pywebview，装一下：")
        print("    pip install pywebview pythonnet")
        return 1

    # 刚才那个 import 把 stdout 换成了 GBK 的 os.devnull（见 _make_output_safe），
    # 这里再兜一次 —— 之后界面这条路上的每一句 print 才真的不会崩。
    _make_output_safe()

    from app.ui.bridge import Api

    actions = {
        "add_text": lambda text: ingest(store, source, pipeline, text),
        "sync": lambda: sync_sources(store, pipeline, registry.sources),
        "ingest_file": lambda path: ingest_file(store, source, pipeline, path),
        "read_file_text": extract_preview,
        "promote_drop": lambda row, paths: promote_dropped(store, pipeline, row, paths),
    }

    # 打包版第一次被双击：桌面上还没有快捷方式就自己建一个。
    # 只补缺：他已经有的（或者故意删掉的）不去动。
    if getattr(sys, "frozen", False):
        try:
            from app.runtime.desktop import (create_shortcut, ensure_autostart,
                                             refresh_icon_cache)

            create_shortcut(only_if_missing=True)
            # 更新完之后有两处会"看着不对"，都在这儿就地修掉：
            #   · 桌面/任务栏图标被系统缓存成白板（用户："一更新就不能用"）
            #   · 开机自启那条指向了旧路径（用户："开机自启动没弄"）
            refresh_icon_cache()
            ensure_autostart()
        except Exception:
            pass                      # 建不了也照常开界面，不是致命问题

    page = RESOURCE / "app" / "ui" / "index.html"
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

    # 标题栏/任务栏图标。打包后 exe 自己也带着同一个图标（打包时嵌进去的）。
    icon = RESOURCE / "assets" / "app.ico"
    api = Api(store, registry.status, actions, notices=STARTUP_NOTES)
    window = webview.create_window("私人秘书", str(page), width=1040, height=680,
                                   min_size=(820, 520), js_api=api)
    # 一键更新要把界面关掉（更新脚本在等这个进程消失）。桥自己拿不到窗口，
    # 所以在这儿递给它 —— 见 bridge.quit_app 和 runtime/selfupdate.py。
    # **名字必须以下划线开头**：pywebview 生成 JS 代理时会 dir() 遍历 js_api，
    # 顺着公开属性一路钻进 WinForms/.NET 对象（Window → BrowserForm → 无障碍
    # 对象），那一步要跨公寓 COM 往返、还握着 GIL —— 界面就"未响应"了，
    # 页面永远停在"正在连接…"（2026-10-14 实锤，py-spy 抓到栈在 util.py:196）。
    api._window = window
    # 走到这儿 = 窗口建出来了，算"能用了"：清掉启动计数，
    # 以后再崩（打游戏被杀、手动结束）不该被当成"打不开"。
    from app.runtime import startup

    startup.mark_ready()
    # 标题栏图标。pywebview 的 create_window 不收这个参数，只有 start 收。
    webview.start(icon=str(icon) if icon.is_file() else None)
    return 0


def list_plugins(statuses) -> int:
    """`--plugins`：装了哪些、哪个坏了。坏插件不影响主程序，但要说出来。"""
    folder = plugin_dir(ROOT)
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
            # 这句要**同时**进 notes：界面上的"同步"只看得见 notes，
            # 打印在窗口模式下是写到 os.devnull 的（用户等于什么都没看到，
            # 只会觉得"点了没反应"）。
            notes.append(f"{label} 拉取失败：{type(exc).__name__}: {exc}")
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
    parser.add_argument("--serve", action="store_true",
                        help="后台全开：收消息 + 定时推送，一个进程（开机自启用这个）")
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
    parser.add_argument("--replay", metavar="MSG_ID",
                        help="把库里某条原始消息重新跑一遍理解层，看现在会解析成什么"
                             "（不入库、不推送）。排查'这条为什么没收下'用。")
    parser.add_argument("--stats", action="store_true", help="看统计")
    parser.add_argument("--plugins", action="store_true",
                        help="看装了哪些插件（plugins/ 文件夹里的 .py）")
    parser.add_argument("--sync", action="store_true",
                        help="拉一遍插件来源（课表订阅这类），存成待办")
    parser.add_argument("--selftest", action="store_true", help="检查契约与环境")
    parser.add_argument("--version", action="store_true",
                        help="看这是第几版（报问题时先跑这个）")
    parser.add_argument("--db", help="数据库路径（默认 data/secretary.db）")
    args = parser.parse_args(argv)

    if args.version:
        from app.version import version_text

        print(version_text(ROOT))
        return 0

    if args.selftest:
        return _selftest()

    # 在压缩包里双击 exe = WinRAR / 7-Zip 把整包解到 %TEMP% 再跑。后果三连，
    # 每一条都有人踩过：配置和待办写在临时目录（关掉就没了）；再解压一份出来
    # 就变成"两份程序抢同一个 18080"；杀软还特别爱拦临时目录里的 exe。
    # 与其让用户对着"端口被占用"猜半天，不如一开始就把话说死。
    if _running_from_archive():
        from app.runtime.desktop import alert

        alert("先解压，再用", (
            "你是在压缩包里直接双击运行的。\n\n"
            "请把整个 zip 解压成一个文件夹，再双击文件夹里的「私人秘书.exe」。\n\n"
            "从压缩包里跑会有三个毛病：配置和待办存在临时目录、关掉就没了；\n"
            "再解压一份出来还会变成两份程序抢同一个端口（就是\"端口被占用\"那个毛病）；\n"
            "杀毒软件也特别喜欢拦临时目录里的程序。"))
        return 1

    # 启动自检 + 安全模式（M0）：
    #   ① 连续 3 次没走到"能用了"，下一次自动跳过插件（至少进得去）；
    #   ② 库坏了就把坏文件改名留档、用新库起来，并告诉用户去备份恢复；
    #   ③ 盘快满了、数据目录写不进去，提前说。
    from app.runtime import preflight, startup

    STARTUP_NOTES[:] = preflight.check(default_data_dir() / "secretary.db")
    SAFE_MODE = ("--safe-mode" in (argv or sys.argv)) or \
        startup.note_attempt() >= startup.LIMIT
    if SAFE_MODE:
        STARTUP_NOTES.insert(0, {
            "code": "APP.SAFE_MODE", "level": "warn",
            "message": "上次启动没走完，这次先进安全模式（暂时不加载插件）。"
                       "配置和待办都在，界面里能正常改设置。"})
        # 安全模式只救一次：清掉计数，下次照常启动。
        # 否则它会**永远**停在安全模式（插件一直不加载），比原来的问题更糟。
        startup.reset()

    # 结构化日志：清掉 7 天前的，把"没人接的异常"也接住（崩了至少有编号和堆栈）。
    from app.runtime import logger

    logger.purge_old()
    for note in STARTUP_NOTES:
        logger.note(note.get("level", "warn"), note.get("code", ""),
                    stage="startup", detail=note.get("message", ""))
    if not SAFE_MODE:
        def _alert_crash(title: str, text: str) -> None:
            from app.runtime.desktop import alert

            alert(title, text)

        logger.install_hooks(alert=_alert_crash)

    # 每天第一次启动顺手备一份库和配置（用户丢过配置、丢过待办）。
    # 放在自检**之后**：库坏了就不能拿坏库去覆盖今天的备份。
    from app.runtime import backup

    backup.run_daily()

    # 关键文件缺了（多半被杀软吃了）：**弹框说清楚**，别让用户对着"双击没反应"发呆。
    # 放在自检后面：`--selftest` 自己会把缺什么列出来，命令行用户不需要弹框。
    missing = missing_app_files()
    if missing:
        alert_missing_files(missing)
        return 1

    # 第一次运行（exe 第一次被双击）：把随包发的插件补到他的 plugins/ 里。
    ensure_bundled_files()

    store, source, pipeline, sink, registry = build(args.db, safe_mode=SAFE_MODE)

    if args.plugins:
        return list_plugins(registry.status)

    if args.sync:
        if not registry.sources:
            print("没有可主动拉取的插件来源。")
            print(f"（插件放在 {plugin_dir(ROOT)}，"
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

    if args.replay:
        return replay_message(store, pipeline, args.replay)

    if args.file:
        target = Path(args.file)
        if not target.is_file():
            print(f"文件不存在：{target}")
            return 1
        # 走 explicit：命令行里主动指一个文件，和界面上点"变成待办"是一个意思，
        # 别让"文件里大多是资料"那道闸把它拦掉。
        message = explicit_file_message(source, str(target))
        added = ingest_message(store, pipeline, message)
        print(f"\n录入完成：新待办 {added} 条")
        for task in store.list_tasks("open", limit=5):
            print(f"  #{task.task_id:<4} {task.title}   [{task.due_at or '没写时间'}]")
        return 0

    if args.qq:
        return run_qq(store, pipeline)

    if args.serve:
        # 没给 --account 就用 config.toml 里 [qq] account 那个号。
        # 界面上的「启动 QQ」按钮走的也是这条路（它不该逼用户记命令行参数）。
        account = args.account or int(config.get("qq", "account", 0) or 0)
        return run_serve(store, pipeline, sink, account or None)

    if args.ui:
        return open_ui(store, source, pipeline, sink, registry)

    if args.shortcut:
        from app.runtime.desktop import create_shortcut

        ok, message = create_shortcut()
        print(("已创建：" if ok else "创建失败：") + message)
        if ok:
            print("注意：快捷方式只打开界面；后台收消息在界面里点「📱 QQ 接收 → 启动」。")
        return 0 if ok else 1

    if args.deploy or args.setup:
        account = args.account or int(config.get("qq", "account", 0) or 0)
        if not account:
            print("要装哪个号？加一个参数，例如：")
            # 用一眼假号做例子：文档/帮助里的号码不该是作者自己的号
            print("    python run.py --deploy --account 123456789")
            print("    （或者在 config.toml 的 [qq] account 里填一次，以后不用再带参数）")
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
        # 双击 exe 不该弹一屏命令行帮助 —— 那就等于"双击没反应"。
        # 打包之后没有任何参数 = 打开界面；源码运行还是打印帮助。
        if getattr(sys, "frozen", False):
            return open_ui(store, source, pipeline, sink, registry)
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

    ensure_bundled_files()      # 打包版：先把自己带的插件补到用户那份 plugins/ 里
    problems: list[str] = []
    for name in ("Message", "TaskDraft", "Task", "PushPayload", "Attachment"):
        if not hasattr(models, name):
            problems.append(f"契约缺数据格式：{name}")
    for iface in (Source, TextEnricher, DraftEnricher, Store, Sink):
        if not getattr(iface, "__abstractmethods__", None):
            problems.append(f"接口没有抽象方法：{iface.__name__}")

    required = {
        "parse": ["filter_strength", "merge_window_seconds", "due_default_hour"],
        "title": ["max_len", "min_meaningful_chars"],
        "store": ["attachment_retention_days", "keep_dropped_days"],
        # 这里以前写的是 default_times —— 那是**早期**的键名，后来改叫 times 了。
        # 之所以一直没暴露：作者自己的 config.toml 里还留着那行老键；
        # 而**新用户**拿到的是新生成的配置，一自检就报"配置缺项"（打包版实测到）。
        "push": ["times", "quote_max_chars", "fallback_text_chars"],
        # 2026-10-10 做 AI 插件时这一段改过键名（cloud_* → base_url/api_key/model）。
        # 又一次"作者的老配置里还留着旧键、新用户一自检就报缺项"——
        # 打包脚本的验证步骤当场抓住了它。
        "ai": ["enabled", "allow_upload", "base_url", "api_key", "model",
               "mode", "min_chars", "timeout_seconds", "token_budget_per_day"],
        "resource": ["mem_critical_percent"],
        "risk": ["confirmed_secondary_account"],
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
    plugins = load_plugins(ROOT, Registry())
    broken = [item for item in plugins if not item.ok]
    if broken:
        problems.append(f"有 {len(broken)} 个插件加载失败（见下面 ✕ 那几行）")

    print("私人秘书 · 自检")
    from app.version import version_text

    print(f"  版本：      {version_text(ROOT)}")
    print(f"  项目根目录：{config.path.parent if config.path else '?'}")
    print(f"  配置文件：  {config.path}")
    print(f"  数据目录：  {data_dir}")
    print(f"  过滤强度：  {config.get('parse', 'filter_strength')}")
    if config.load_note:
        print(f"  ⚠️ {config.load_note}")
    # AI 是插件，关着就是纯规则解析。**如实说它是开着还是关着** ——
    # 打印"AI 模式：conservative"那种话，用户会以为它一直在跑。
    if config.get("ai", "enabled", False) and config.get("ai", "allow_upload", False):
        print(f"  AI 增强：   开着（{config.get('ai', 'model') or '没填模型'}"
              f" @ {config.get('ai', 'base_url') or '没填地址'}）")
    else:
        print("  AI 增强：   关着（纯规则解析：不联网、不花钱、内容不出去）")
    print(f"  插件：      {len(plugins)} 个（{plugin_dir(ROOT)}）")
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
