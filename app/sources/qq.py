"""QQ 来源：接 NapCat（OneBot v11 反向 WebSocket）。

=====================================================================
通信方向（"反向"是这个意思）
=====================================================================
     NapCat（QQ 里的插件）  ──连过来──►  本程序 127.0.0.1:18080/onebot/v11/ws

是**我们监听端口、它来连我们**，不是我们去连它。所以配置写在两边都要对得上：
程序这边 config.toml 的 [qq] bind_host/bind_port/path/token，
NapCat 那边填"反向 WebSocket 地址"和同一个 token。

=====================================================================
安全姿态（宁可不做也不冒险）
=====================================================================
  · **只收不发**：不主动发 QQ 消息、不标记已读、不拉群成员列表。
    少一个动作就少一分被腾讯盯上的概率。
  · 三道关：路径要对 → 来源必须是本机回环地址 → token 要对。
  · 同一时刻只接受一个连接（NapCat 重连时会先踢掉旧的）。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  改"收哪些群"             → 不用改代码，改 config.toml 的 [qq] group_ids
  改消息怎么拼成文字        → `flatten_message()`（OneBot 各种消息段在这里处理）
  改"什么样的事件值得处理"  → `event_to_message()`（白名单判断在这里）
  改端口 / 路径 / 令牌       → config.toml 的 [qq]
  改"闲聊要不要收"          → 不在这里！在 pipeline/chat_filter.py
  加图片/文件/语音下载       → 第 5 步；本文件已经识别出它们，只是还没下载

=====================================================================
为什么群噪声过滤不在这里
=====================================================================
契约规定"来源只负责把外面的东西搬进来，不做解析"。判"这句话值不值得变待办"
要用时间识别，那是理解层的活，放在 pipeline/chat_filter.py。
这样 QQ 接入能整块换掉（比如换成别的协议）而不影响判断规则。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from ..contracts.interfaces import Source
from ..contracts.models import (
    SOURCE_QQ_GROUP,
    SOURCE_QQ_INBOX,
    TIME_FORMAT,
    Message,
)
from ..runtime.config import config

# 来源标识。QQ 一个源会产出两种消息：
#   qq_group  群消息（别人发的，要过闲聊过滤）
#   qq_inbox  小号收件箱（自己发给小号的，不过滤）
SOURCE_QQ = "qq"
KIND_GROUP = "group"
KIND_PRIVATE = "private"


# ===========================================================================
# 消息段拍平：OneBot 的 message 数组 → 一段人话
# ===========================================================================
def flatten_message(event: dict[str, Any], self_id: str
                    ) -> tuple[str, bool, list[str], list[str]]:
    """把 OneBot 的消息段拼成纯文本。

    返回 (文本, 有没有@我, 图片地址列表, 语音地址列表)。

    为什么要自己拼而不用 raw_message：
      不同 QQ 版本 / NapCat 版本给的字段不一样，有的直接给一段 CQ 码字符串，
      有的给结构化的数组。统一处理成"人话 + 方括号占位符"，
      后面的规则才能一套走到底。
    """
    mentioned = False
    parts: list[str] = []
    images: list[str] = []
    voices: list[str] = []
    message = event.get("message")
    if isinstance(message, str):        # 有些实现直接给 CQ 码字符串
        message = [{"type": "text", "data": {"text": message}}]
    for segment in message or []:
        if not isinstance(segment, dict):
            continue
        kind = segment.get("type")
        data = segment.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "at":
            qq = str(data.get("qq") or "")
            if qq == self_id and self_id:
                mentioned = True
                parts.append("@我 ")
            elif qq == "all":
                parts.append("@全体 ")
            else:
                parts.append("@某人 ")
        elif kind == "image":
            parts.append("[图片]")
            for key in ("url", "file"):
                value = str(data.get(key) or "")
                if value.startswith("http"):
                    images.append(value)
                    break
        elif kind == "face":
            parts.append("[表情]")
        elif kind == "reply":
            parts.append("[回复]")
        elif kind == "file":
            parts.append(f"[文件 {data.get('name') or ''}]")
        elif kind in ("json", "xml"):
            parts.append(card_text(data))       # 闪传 / 分享链接这类卡片
        elif kind == "flashtransfer":
            parts.append("[闪传文件]")
        elif kind == "record":
            parts.append("[语音]")
            for key in ("url", "file", "path"):
                value = str(data.get(key) or "")
                if value:
                    voices.append(value)
                    break
        elif kind == "forward":
            parts.append("[合并转发]")
    text = "".join(parts).strip()

    if not text:
        # 兜底：有些实现只给 raw_message 的 CQ 码串。
        # 先把 [CQ:xxx,...] 摘掉 —— 否则标题会变成一串看不懂的代码，
        # 旧库里真出现过这种待办。
        raw = str(event.get("raw_message") or "").strip()
        if raw:
            text = re.sub(r"\[CQ:[^\]]*\]", " ", raw).strip()
    return text, mentioned, images, voices


def card_text(data: dict[str, Any]) -> str:
    """卡片消息（json / xml 段）里能读出来的人话。

    QQ「闪传」、分享链接这类都走 json 段，JSON 里通常带 prompt / title / desc，
    例如 "[QQ闪传] 学生守则"。取不到就退成 [卡片消息]。
    """
    for key in ("prompt", "title", "desc", "summary"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    raw = str(data.get("data") or "")
    if raw:
        try:
            parsed = json.loads(raw)
            for key in ("prompt", "title", "desc", "summary"):
                value = str(parsed.get(key) or "").strip()
                if value:
                    return value
            meta = parsed.get("meta") or {}
            for group in meta.values():
                if isinstance(group, dict):
                    for key in ("title", "desc"):
                        value = str(group.get(key) or "").strip()
                        if value:
                            return value
        except (ValueError, TypeError, AttributeError):
            pass
    return "[卡片消息]"


# ===========================================================================
# 会话身份
# ===========================================================================
def chat_identity(event: dict[str, Any]) -> tuple[str, str, str]:
    """这条事件属于哪个会话。

    返回 (kind, 会话号, 显示名)：kind 是 "group" 或 "private"；
    群聊的会话号是群号，私聊的会话号是对方 QQ 号。
    """
    message_type = str(event.get("message_type") or "")
    sender = event.get("sender") or {}
    nickname = str(sender.get("card") or sender.get("nickname") or "").strip()
    if message_type == "private":
        raw = event.get("user_id")
        try:
            ref = str(int(raw)) if int(raw) > 0 else ""
        except (TypeError, ValueError):
            ref = ""
        return KIND_PRIVATE, ref, nickname
    if message_type == "group":
        raw = event.get("group_id")
        try:
            ref = str(int(raw)) if int(raw) > 0 else ""
        except (TypeError, ValueError):
            ref = ""
        name = str(event.get("group_name") or sender.get("group_name") or "").strip()
        return KIND_GROUP, ref, name
    return "", "", ""


def self_message_target(event: dict[str, Any], self_id: str) -> str | None:
    """这条是不是"我自己发出去的"？是就返回真实收件会话号，不是返回 None。

    为什么需要单独判断：
      NapCat（OneBot 通用做法）判断"自己发的消息"用的是 `user_id == 自己的号`，
      真实收件人另外放在 `target_id`。不认这个的话，"发给自己小号"全被当成
      别人的消息、或者被当成非消息事件扔掉。

    这也是旧项目踩过的坑：一开始只认 post_type == "message"，
    而自己发出去的消息类型是 "message_sent"。
    """
    if not self_id:
        return None
    owner = str(event.get("user_id") or "")
    sender_id = str((event.get("sender") or {}).get("user_id") or "")
    if owner != str(self_id) and sender_id != str(self_id):
        return None                      # 别人发来的
    target = str(event.get("target_id") or "")
    if target and target != "0":
        return target
    if str(event.get("message_type")) == KIND_GROUP:
        return str(event.get("group_id") or "")
    return ""                            # 私聊但拿不到收件人


def today_allday() -> str:
    """没写时间的随手记：按"今天全天"记。

    用当天 23:59 表示"全天"—— 这样它今天一直待在"今天/本周"里，
    不会刚记下就被算成逾期；跨天没做完才会进"逾期"。
    """
    return datetime.now().strftime("%Y-%m-%d 23:59:00")


# ===========================================================================
# 事件 → Message
# ===========================================================================
def event_to_message(event: dict[str, Any], settings: "QQSettings",
                     self_id: str = "") -> tuple[Message | None, str]:
    """OneBot 事件 → 标准 Message。返回 (消息, 跳过原因)。

    这一步只做两件事：
      1. 筛"这个会话在不在接收范围里"（群白名单 / 收件箱）；
      2. 把消息段拍成人话。
    "这句话值不值得变待办"不在这里 —— 见 pipeline/chat_filter.py。
    """
    post_type = str(event.get("post_type") or "")
    if post_type not in ("message", "message_sent"):
        return None, "非消息事件"

    chat_kind, chat_ref, chat_name = chat_identity(event)
    target = self_message_target(event, self_id)

    inbox = False
    if target is not None:
        # 自己发出去的：只有"收件箱"名单里的会话才收。
        # 否则你在群里、或跟别人私聊时随口说的话也会被当成待办。
        if target not in {str(i) for i in settings.inbox_ids}:
            return None, f"自己发的消息，收件会话 {target or '未知'} 不在收件箱名单里"
        inbox = True
        if chat_kind == KIND_GROUP:
            chat_ref = str(event.get("group_id") or chat_ref)
            chat_name = chat_name or ""
        else:
            # 私聊发出去：把会话身份换成真实收件人（小号），
            # 这样来源、去重、界面列表都指向那个会话，而不是统统记到"自己"名下。
            chat_kind = KIND_PRIVATE
            chat_ref = target
            chat_name = ""
    elif chat_kind == KIND_GROUP:
        if event.get("sub_type") == "anonymous":
            return None, "匿名消息"
        if not chat_ref:
            return None, "缺少群号"
        if not settings.receive_all and int(chat_ref) not in settings.group_ids:
            return None, f"群 {chat_ref} 没有勾选接收"
    elif chat_kind == KIND_PRIVATE:
        if not chat_ref:
            return None, "缺少用户号"
        if not settings.receive_private_all and int(chat_ref) not in settings.private_ids:
            return None, f"私聊 {chat_ref} 没有勾选接收"
    else:
        return None, "既不是群聊也不是私聊"

    text, mentioned, images, _voices = flatten_message(
        event, self_id or str(event.get("self_id") or ""))
    if settings.only_mention and not mentioned and not inbox:
        return None, "没有@我"

    if len(text) > settings.max_text_chars:
        text = text[: settings.max_text_chars]

    sender_info = event.get("sender") or {}
    nickname = str(sender_info.get("card") or sender_info.get("nickname")
                   or sender_info.get("user_id") or "")
    if chat_kind == KIND_PRIVATE:
        display = (f"私聊 {chat_ref}（我发的）" if inbox
                   else f"私聊 {nickname or chat_name or chat_ref}")
    else:
        display = (str(event.get("group_name") or sender_info.get("group_name") or "")
                   or chat_name or f"群{chat_ref}")

    msg_id = str(event.get("message_id") or "")
    message = Message(
        msg_id=f"{SOURCE_QQ}:{msg_id}" if msg_id else f"{SOURCE_QQ}:{time.time_ns()}",
        source_kind=SOURCE_QQ_INBOX if inbox else SOURCE_QQ_GROUP,
        source_name=display,
        sender=nickname,
        is_self=inbox,
        sent_at=_event_time(event),
        text=text,
        # 附件（图片/文件/语音的下载）是第 5 步的事。
        # 这里先把地址记进 raw，免得后面要重新解析一遍事件。
        attachments=[],
        raw={
            "qq_kind": chat_kind,
            "qq_ref": chat_ref,
            "mentioned": mentioned,
            "image_urls": images,
            "message_id": msg_id,
        },
    )
    return message, ""


def _event_time(event: dict[str, Any]) -> str:
    """事件里的时间戳 → 标准时间字符串。拿不到就用现在。"""
    stamp = event.get("time")
    try:
        if stamp is not None:
            return datetime.fromtimestamp(int(stamp)).strftime(TIME_FORMAT)
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    return datetime.now().strftime(TIME_FORMAT)


# ===========================================================================
# 配置
# ===========================================================================
def _parse_ids(raw: Any) -> list[int]:
    """把 [123, 456] 或 "123,456" 解析成号码列表，非法片段直接丢掉。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        pieces = [str(x) for x in raw]
    else:
        pieces = str(raw).replace("，", ",").split(",")
    out: list[int] = []
    for piece in pieces:
        piece = piece.strip()
        if piece.lstrip("-").isdigit():
            value = int(piece)
            if value > 0:
                out.append(value)
    return out


def _as_int(raw: Any, default: int) -> int:
    """读一个整数配置；写坏了也不要让整个程序起不来。"""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class QQSettings:
    """QQ 接入的全部设置。来源：config.toml 的 [qq] 段。"""

    enabled: bool = False
    bind_host: str = "127.0.0.1"
    bind_port: int = 18080
    path: str = "/onebot/v11/ws"
    token: str = ""
    group_ids: list[int] = field(default_factory=list)
    receive_all: bool = False
    private_ids: list[int] = field(default_factory=list)
    receive_private_all: bool = False
    inbox_ids: list[int] = field(default_factory=list)
    only_mention: bool = False
    max_text_chars: int = 3000

    @classmethod
    def from_config(cls) -> "QQSettings":
        return cls(
            enabled=bool(config.get("qq", "enabled", False)),
            bind_host=str(config.get("qq", "bind_host", "127.0.0.1") or "127.0.0.1"),
            bind_port=_as_int(config.get("qq", "bind_port", 18080), 18080),
            path=str(config.get("qq", "path", "/onebot/v11/ws") or "/onebot/v11/ws"),
            token=str(config.get("qq", "token", "") or ""),
            group_ids=_parse_ids(config.get("qq", "group_ids", [])),
            receive_all=bool(config.get("qq", "receive_all", False)),
            private_ids=_parse_ids(config.get("qq", "private_ids", [])),
            receive_private_all=bool(config.get("qq", "receive_private_all", False)),
            inbox_ids=_parse_ids(config.get("qq", "inbox_ids", [])),
            only_mention=bool(config.get("qq", "only_mention", False)),
            max_text_chars=_as_int(config.get("qq", "max_text_chars", 3000), 3000),
        )

    def summary(self) -> str:
        """一句话说明当前接收范围，给自检和界面显示。

        只讲"收哪些"，不讲"监听在哪" —— 地址在别处单独打印，
        混在一起会重复一遍。
        """
        groups = "全部群" if self.receive_all else f"{len(self.group_ids)} 个群"
        privates = ("全部私聊" if self.receive_private_all
                    else f"{len(self.private_ids)} 个私聊")
        inbox = f"{len(self.inbox_ids)} 个收件箱"
        scope = "只看@我" if self.only_mention else "全部消息"
        return f"{groups}｜{privates}｜{inbox}｜{scope}"


def ensure_token(current: str = "") -> str:
    """没填令牌就生成一串随机的。

    为什么要令牌：端口开着，万一机器上别的程序也能连本机回环，
    没有令牌它就能往我们这儿灌假消息。生成一次、填进 NapCat 两边对得上就行。
    """
    token = str(current or "").strip()
    return token or secrets.token_urlsafe(24)


# ===========================================================================
# QQSource：契约里的 Source 实现
# ===========================================================================
class QQSource(Source):
    """QQ 来源。

    契约规定 `kind` 是来源唯一标识，所以这里是 "qq"；
    但一个 QQ 源会产出两种 source_kind 的消息：
        群消息 → qq_group（要过闲聊过滤）
        收件箱 → qq_inbox（不过滤）
    这是唯一一个"一个源、两种消息"的来源，属于正常情况。

    用法：
        src = QQSource(settings, on_message=处理函数, on_status=界面回调)
        src.start()        # 起一个后台线程监听
        ...
        src.stop()
    测试可以直接调 `feed_event(event)` —— 不需要真的开端口。
    """

    kind = SOURCE_QQ
    title = "QQ 消息"

    def __init__(
        self,
        settings: QQSettings | None = None,
        on_message: Callable[[Message], Any] | None = None,
        on_status: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings or QQSettings.from_config()
        self.on_message = on_message
        self.on_status = on_status
        self.self_id = ""
        self._status: dict[str, Any] = {
            "running": False,        # 端口在监听吗
            "connected": False,      # NapCat 连上了吗
            "self_id": "",           # 登录的 QQ 号（NapCat 报上来的）
            "received": 0,           # 收到多少条
            "accepted": 0,           # 收下多少条
            "skipped": 0,            # 拦掉多少条
            "last_reason": "",       # 最后一条为什么被拦
            "error": "",             # 出错信息
        }
        self._connection: Any = None
        self._server: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    # ------------------------------------------------------------------ 契约
    def submit(self, message: Message) -> Message:
        """契约要求的方法。QQ 是推模式，正常不用手动 submit。"""
        return message

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict[str, Any]:
        return dict(self._status)

    def _set(self, **fields: Any) -> None:
        self._status.update(fields)
        if self.on_status is not None:
            try:
                self.on_status(self.status())
            except Exception:
                pass

    def describe(self) -> str:
        """一句话说明当前状态，给人看。"""
        if not self.settings.enabled:
            return "QQ 接入：未启用（config.toml 的 [qq] enabled = false）"
        state = self._status
        if state.get("error"):
            return f"QQ 接入：{state['error']}"
        if state.get("connected"):
            return (f"QQ 接入：已连接｜登录号 {state.get('self_id') or '?'}｜"
                    f"收到 {state.get('received', 0)} 条，收下 {state.get('accepted', 0)} 条")
        if state.get("running"):
            return f"QQ 接入：已监听 {self.settings.bind_port}，等 NapCat 连接"
        return "QQ 接入：未启动"

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        """起一个后台线程跑事件循环。返回是否成功起线程。"""
        if not self.settings.enabled:
            self._set(error="[qq] enabled = false，没有启动")
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stopping = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="qq-source", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """停服务：关连接、关端口、停线程。"""
        self._stopping = True
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(timeout=5)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        self._set(running=False, connected=False)

    def _run(self) -> None:
        """后台线程主体：跑事件循环。"""
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
            self._loop.run_forever()
        except Exception as exc:
            self._set(running=False, error=f"事件服务异常：{type(exc).__name__}")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _serve(self) -> None:
        if await self._bind():
            return
        # 端口可能被别的进程短暂占用（比如刚重启过），过一会儿自己重试，
        # 省得用户手动重启程序。
        asyncio.create_task(self._retry_bind())

    async def _bind(self) -> bool:
        """开监听。端口被占用时返回 False（不抛异常，交给重试）。"""
        try:
            from websockets.asyncio.server import serve
        except ImportError:
            self._set(error="缺依赖：pip install websockets")
            return True          # 不用重试了，装完再说
        try:
            self._server = await serve(
                self._handler,
                self.settings.bind_host,
                self.settings.bind_port,
                ping_interval=None,        # NapCat 自己会保活，我们别插一脚
                max_size=4 * 1024 * 1024,
                max_queue=1024,
            )
            self._set(running=True, error="")
            return True
        except OSError as exc:
            self._set(running=False, error=(
                f"端口 {self.settings.bind_port} 被占用"
                f"（{exc.strerror or exc}），稍后自动重试"))
            return False

    async def _retry_bind(self, attempts: int = 20, interval: float = 15.0) -> None:
        """绑不上就每 15 秒再试，最多 5 分钟。"""
        for _ in range(attempts):
            await asyncio.sleep(interval)
            if self._stopping or self._server is not None:
                return
            if await self._bind():
                return

    async def _shutdown(self) -> None:
        """关连接、关监听。"""
        if self._connection is not None:
            try:
                await self._connection.close(code=1001, reason="service shutdown")
            except Exception:
                pass
            self._connection = None
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    # ------------------------------------------------------------------ 接入校验
    def _authorized(self, connection: Any) -> tuple[bool, str]:
        """三道关：路径、来源 IP、令牌。任何一道不过就拒。

        默认只接受本机回环地址 —— 这样即使路由器把端口映射到了公网，
        外面的机器也连不进来。
        """
        request = getattr(connection, "request", None)
        raw_path = str(getattr(request, "path", "") or "")
        parsed = urlsplit(raw_path)
        if parsed.path != self.settings.path:
            return False, f"路径不对：{parsed.path}"

        remote = getattr(connection, "remote_address", None)
        try:
            client_ip = ipaddress.ip_address(str(remote[0]))
        except (TypeError, ValueError, IndexError):
            return False, "取不到来源 IP"
        if not client_ip.is_loopback:
            return False, f"非本机来源：{client_ip}"

        if self.settings.token:
            query = parse_qs(parsed.query).get("access_token", [""])[0]
            headers = getattr(request, "headers", {}) or {}
            auth = str(headers.get("Authorization") or "")
            bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            if self.settings.token not in (query, bearer):
                return False, "Token 不匹配"
        return True, ""

    # ------------------------------------------------------------------ 连接处理
    async def _handler(self, connection: Any) -> None:
        """一条 NapCat 连接的一生。"""
        ok, reason = self._authorized(connection)
        if not ok:
            self._set(error=f"拒绝了一个连接：{reason}")
            await connection.close(code=1008, reason="unauthorized")
            return
        if self._connection is not None:
            # 已经有一条了：让新的进来，旧的踢掉（NapCat 重连时会出现这种情况）
            try:
                await connection.close(code=1013, reason="already connected")
            except Exception:
                pass
            return

        self._connection = connection
        self._set(connected=True, error="")
        try:
            async for raw in connection:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                try:
                    event = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                post_type = str(event.get("post_type") or "")
                if post_type == "meta_event":
                    # 心跳和生命周期事件：只记一下登录的 QQ 号
                    self_id = str(event.get("self_id") or "")
                    if self_id:
                        self.self_id = self_id
                        self._set(self_id=self_id)
                    continue
                if post_type in ("notice", "request"):
                    continue          # 戳一戳/撤回/加好友这类不是消息
                self.feed_event(event)
        except Exception as exc:
            self._set(error=f"连接异常：{type(exc).__name__}")
        finally:
            self._connection = None
            self._set(connected=False)

    # ------------------------------------------------------------------ 事件处理
    def feed_event(self, event: dict[str, Any]) -> tuple[Message | None, str]:
        """处理一条 OneBot 事件。返回 (消息, 跳过原因)。

        这个方法不碰网络，所以测试可以直接喂假事件进来 ——
        不用真的开端口、不用真的登 QQ。
        """
        self._set(received=self._status["received"] + 1)
        self_id = self.self_id or str(event.get("self_id") or "")
        message, reason = event_to_message(event, self.settings, self_id)
        if message is None:
            self._set(skipped=self._status["skipped"] + 1, last_reason=reason)
            return None, reason
        self._set(accepted=self._status["accepted"] + 1, last_reason="")
        if self.on_message is not None:
            try:
                self.on_message(message)
            except Exception as exc:
                self._set(error=f"处理消息失败：{type(exc).__name__}: {exc}")
                return message, f"处理失败：{type(exc).__name__}"
        return message, ""
