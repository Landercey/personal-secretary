"""企业微信「群机器人」推送。只要一个 Webhook 地址，其它什么都不用配。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  改推送文案格式         → `build_digest()`（在 sinks/console.py 里，两处共用）
  改单条消息长度上限     → 本文件顶部 TEXT_LIMIT / MARKDOWN_LIMIT
  改每分钟最多发几条     → config.toml 的 [wecom_bot] max_per_minute
  改"XX 错误码是什么意思" → 本文件底部 BOT_ERROR_HINTS
  改图片/文件怎么发       → `send_image()` / `send_file()`
  改超时、重试次数       → config.toml 的 [wecom_bot] timeout / retry_times
  换成钉钉/飞书           → 照抄本文件，实现 Sink 接口，改 sinks/router.py 的挑选逻辑

=====================================================================
为什么不用 requests 这个库
=====================================================================
官方接口就是「POST 一段 JSON」和「POST 一段 multipart 表单」，标准库的
urllib 两样都能干。少一个第三方依赖，打包体积小一点，用户装的时候少一个出错点 ——
这个项目要能在 4 GB 机器上十分钟跑起来。

=====================================================================
群机器人的硬限制（官方定的，改不了）
=====================================================================
  · 只能**发**，不能收 —— 用户在企微里点按钮，程序收不到（CONTRACT.md 第二节）
  · text 最长 2048 字节，markdown 最长 4096 字节（按 UTF-8 字节算，不是字数）
  · 每个机器人每分钟最多 20 条，超了报错 45009
  · 图片 base64 之后不能超过 2 MB；文件走"先上传换 media_id"的路子，20 MB 以内
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from ..contracts.interfaces import Sink, SinkResult
from ..contracts.models import KIND_IMAGE, PushPayload

# ---------------------------------------------------------------------------
# 官方接口地址。腾讯改地址的话，只改这两行。
# ---------------------------------------------------------------------------
WEBHOOK_BASE = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
UPLOAD_BASE = "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media"

# ---------------------------------------------------------------------------
# 消息长度上限（字节）。官方文档写的是字节，不是字数 ——
# 一个汉字算 3 字节，所以 2048 字节其实只有 600 多个汉字。
# ---------------------------------------------------------------------------
TEXT_LIMIT = 2048
MARKDOWN_LIMIT = 4096
# 图片 base64 之后不能超过 2M
IMAGE_BASE64_LIMIT = 2_000_000

# 触发频率限制时官方给的错误码
RATE_ERROR = 45009

# ---------------------------------------------------------------------------
# 错误码 → 人话。用户看到"发送失败"是不知所措的，得告诉他到底哪儿不对。
# 官方错误码表变动不频繁；遇到没登记的错误码，会原样把 errmsg 显示出来。
# ---------------------------------------------------------------------------
BOT_ERROR_HINTS = {
    93000: "Webhook 地址无效：机器人被删了，或者地址没复制全",
    40001: "凭证无效",
    40058: "消息内容格式不对或太长",
    45009: "触发频率限制（每个机器人每分钟最多 20 条）",
    45033: "机器人已被停用",
    60020: "本机 IP 不在企业可信 IP 名单里（家用宽带会变，一般无解）",
}


# ===========================================================================
# 两个纯函数：不碰网络，所以能直接跑测试
# ===========================================================================
def split_webhook(raw: str) -> str:
    """从用户粘贴的那一坨里，把真正的 key 抠出来。

    为什么不能简单地 `split("key=")[1]`：
      用户复制地址时经常出岔子，实测遇到过三种：
        1. 整条粘对了：      https://.../send?key=abc-123
        2. 丢了问号：        https://.../sendkey=abc-123   （?? 后面直接跟 key=）
        3. 只复制了 key：    abc-123
      三种都得认，否则用户会以为"我明明复制了"。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if "key=" in text:
        try:
            key = (parse_qs(urlsplit(text).query).get("key") or [""])[0].strip()
        except ValueError:
            key = ""
        if key:
            return key
        # 兜底：不按 URL 解析，直接从 "key=" 之后抠到 & 或空格为止
        tail = text.split("key=", 1)[1]
        return tail.split("&")[0].split()[0].strip()
    # 纯 key：只允许字母数字和 - _ ，避免把一段乱码当成 key
    if all(ch.isalnum() or ch in "-_" for ch in text):
        return text
    return ""


def fit_bytes(text: str, limit: int) -> str:
    """按**字节**截断，且不会把一个汉字劈成半个。

    直接 `text[:600]` 是按"字符数"切的，一个汉字 3 字节，
    600 个汉字就是 1800 字节 —— 看起来没超，实际上超了或者没切够。
    这里 encode 之后按字节切，切完往前退到能解码的位置。
    """
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    keep = raw[: max(0, limit - 3)]        # 留 3 字节给结尾的省略号
    while keep:
        try:
            return keep.decode("utf-8") + "…"
        except UnicodeDecodeError:
            keep = keep[:-1]                # 正好切在汉字中间，少要一个字节
    return "…"


def split_for_send(text: str, limit: int) -> list[str]:
    """超长内容切成多条，优先在换行处切。

    为什么需要：一天的汇总如果有 30 条待办，4096 字节根本装不下。
    在换行处切，手机上看起来是一条条消息，不是一个被砍断的半句话。
    """
    if len(text.encode("utf-8")) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate.encode("utf-8")) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # 单行自己就超长（比如一整段没换行的长文）：按字节硬切
        while len(line.encode("utf-8")) > limit:
            head = fit_bytes(line, limit)
            chunks.append(head)
            line = line[len(head.rstrip("…")):]
        current = line
    if current:
        chunks.append(current)
    return chunks or [""]


def _multipart(file_field: str, file_name: str, file_bytes: bytes) -> tuple[bytes, str]:
    """手工拼一个 multipart/form-data 请求体。

    上传文件必须用这种格式，标准库里没有现成的构造器，所以手拼。
    返回 (请求体字节, Content-Type 头里的 boundary 值)。

    这里**故意不收 fields 参数**：企微的 upload_media 接口所有参数
    （key、type）都走 URL 查询串，表单体里只有文件本身。
    留一个用不上的参数，早晚有人往里塞东西然后调不通。
    """
    boundary = "----Secretary" + uuid.uuid4().hex
    body = bytearray()
    mime = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    body += f"--{boundary}\r\n".encode()
    body += (f'Content-Disposition: form-data; name="{file_field}"; '
             f'filename="{file_name}"\r\n').encode()
    body += f"Content-Type: {mime}\r\n\r\n".encode()
    body += file_bytes
    body += f"\r\n--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


# ===========================================================================
# 推送通道本体
# ===========================================================================
class WeComBotSink(Sink):
    """企业微信群机器人。实现契约里的 Sink 接口，所以能被随时换掉。"""

    name = "wecom_bot"

    def __init__(
        self,
        webhook: str = "",
        max_per_minute: int = 20,
        retry_times: int = 2,
        timeout: float = 10.0,
        enabled: bool = True,
        window_seconds: float = 60.0,
        rate_wait_seconds: float = 61.0,
        # 下面两个参数只为了测试：测试里换成假的时钟和假的 sleep，
        # 就无需真的等 60 秒。生产上不传，用默认的。
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.key = split_webhook(webhook)
        self.webhook = (webhook or "").strip()
        self.max_per_minute = max(1, int(max_per_minute or 20))
        self.retry_times = max(0, int(retry_times or 0))
        self.timeout = float(timeout or 10)
        self.enabled = bool(enabled)
        self.window_seconds = float(window_seconds or 60.0)
        self.rate_wait_seconds = float(rate_wait_seconds or 61.0)
        self.last_error = ""
        self._sent: deque[float] = deque()      # 最近一分钟内每条消息的发出时刻
        self._rate_lock = threading.Lock()
        self._now = now
        self._sleep = sleep

    # ------------------------------------------------------------------ 配置
    @classmethod
    def from_config(cls) -> "WeComBotSink":
        """按 config.toml 里的 [wecom_bot] 建一个通道。

        ⚠️ `config` 要**在函数里**导入。写在文件顶上就会在 import 那一刻
        绑死成当时那个对象，之后程序换了配置它还是看旧的 —— 表现是
        "界面明明配好了，推送还是按老的来"。
        """
        from ..runtime.config import config

        return cls(
            webhook=str(config.get("wecom_bot", "webhook", "") or ""),
            max_per_minute=int(config.get("wecom_bot", "max_per_minute", 20) or 20),
            retry_times=int(config.get("wecom_bot", "retry_times", 2) or 0),
            timeout=float(config.get("wecom_bot", "timeout", 10) or 10),
            enabled=bool(config.get("wecom_bot", "enabled", False)),
        )

    def ready(self) -> bool:
        """能发了吗？—— 开着 + 填了 Webhook。"""
        return self.enabled and bool(self.key)

    def describe(self) -> str:
        """一句话说明当前状态，给设置界面和自检用。"""
        if not self.enabled:
            return "群机器人推送：未启用"
        if not self.key:
            return "群机器人推送：还没填 Webhook 地址"
        # 只露最后 4 位：够用户确认"是不是这个机器人"，又不至于截图/日志里
        # 把整条密钥漏出去（有密钥就能往群里发消息）。
        tail = self.key[-4:] if len(self.key) > 8 else "☓☓☓☓"
        return f"群机器人推送：已启用｜key …{tail}｜限速 {self.max_per_minute} 条/分钟"

    # ------------------------------------------------------------------ 网络
    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST 一段 JSON 到群机器人地址。"""
        url = f"{WEBHOOK_BASE}?key={self.key}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post_form(self, url: str, body: bytes, content_type: str) -> dict[str, Any]:
        """POST 一段 multipart 表单（上传文件用）。"""
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": content_type})
        with urllib.request.urlopen(request, timeout=max(self.timeout, 30.0)) as response:
            return json.loads(response.read().decode("utf-8"))

    # ------------------------------------------------------------------ 节流
    def _throttle(self) -> None:
        """发之前先问一句"这一分钟还有额度吗"，没有就等到有。

        为什么一定要本地自己限速：
          超了官方会直接报 45009，而且那条消息就丢了。本地排队比事后重试稳。
        """
        while True:
            with self._rate_lock:
                now = self._now()
                while self._sent and now - self._sent[0] >= self.window_seconds:
                    self._sent.popleft()
                if len(self._sent) < self.max_per_minute:
                    self._sent.append(now)
                    return
                wait = self.window_seconds - (now - self._sent[0]) + 0.05
            self._sleep(min(max(wait, 0.05), 5.0))

    # ------------------------------------------------------------------ 发送
    def deliver(self, payload: PushPayload) -> SinkResult:
        """契约入口：把一条 PushPayload 送出去（正文 + 附件）。"""
        if not self.ready():
            reason = "群机器人推送未启用" if not self.enabled else "还没填 Webhook 地址"
            return SinkResult(ok=False, code=-1, message=reason)

        limit = MARKDOWN_LIMIT if payload.kind == "markdown" else TEXT_LIMIT
        body = payload.body or payload.title or ""
        # 超长就切成多条发 —— 一天的汇总装不下 4096 字节是常态。
        result = SinkResult(ok=True, message="ok")
        for index, chunk in enumerate(split_for_send(body, limit)):
            text = f"{payload.title}\n{chunk}" if payload.title and index == 0 else chunk
            result = (self.send_markdown(text) if payload.kind == "markdown"
                      else self.send_text(text))
            if not result.ok:
                return result

        # 附件：图片走图片接口，其它走文件接口。
        # 附件发失败**不影响**正文已经发出去这件事，只记备注 ——
        # 推送是锦上添花，不能把主线带崩。
        from ..runtime.config import config

        if not bool(config.get("wecom_bot", "send_attachments", True)):
            return result
        failures: list[str] = []
        for attachment in payload.attachments:
            if not attachment.exists():
                failures.append(f"{attachment.name}（文件不在了）")
                continue
            sent = (self.send_image(attachment.path)
                    if attachment.kind == KIND_IMAGE else self.send_file(attachment.path))
            if not sent.ok:
                failures.append(f"{attachment.name}（{sent.message}）")
        if failures:
            result = SinkResult(ok=True, code=0,
                                message="正文已发；附件没发成：" + "、".join(failures))
        return result

    def send_text(self, content: str) -> SinkResult:
        """发纯文本。超长会自动按字节截断。"""
        text = fit_bytes(str(content or "").strip(), TEXT_LIMIT)
        if not text:
            return SinkResult(ok=False, code=-1, message="内容为空，不发送")
        return self._deliver({"msgtype": "text", "text": {"content": text}})

    def send_markdown(self, content: str) -> SinkResult:
        """发 markdown（能带粗体、链接；上限 4096 字节）。"""
        text = fit_bytes(str(content or "").strip(), MARKDOWN_LIMIT)
        if not text:
            return SinkResult(ok=False, code=-1, message="内容为空，不发送")
        return self._deliver({"msgtype": "markdown", "markdown": {"content": text}})

    def send_image(self, path: str) -> SinkResult:
        """发一张图片。

        官方要的是「图片内容的 base64 + 它的 md5」。超过 2 MB 直接不收 ——
        这里不自己压缩（压缩要引第三方库），而是老实告诉用户发不了。
        """
        target = Path(path)
        if not target.is_file():
            return SinkResult(ok=False, code=-1, message="图片文件不在了")
        data = target.read_bytes()
        encoded = base64.b64encode(data).decode("ascii")
        if len(encoded) > IMAGE_BASE64_LIMIT:
            return SinkResult(ok=False, code=-1, message="图片太大（超 2 MB），企业微信不收")
        return self._deliver({
            "msgtype": "image",
            "image": {"base64": encoded, "md5": hashlib.md5(data).hexdigest()},
        })

    def send_file(self, path: str) -> SinkResult:
        """发一个文件（Word / PDF / 压缩包…）。

        两步：先上传换一个 media_id，再拿 media_id 发出去。

        ⚠️ 踩过的坑：`type=file` 必须放在 **URL 的查询参数**里，
        不能放进 multipart 表单体 —— 放表单里官方会回
        `40058 missing param 'type'`，看起来像是"消息格式不对"，
        其实跟消息内容毫无关系，很容易查错方向。
        """
        target = Path(path)
        if not target.is_file():
            return SinkResult(ok=False, code=-1, message="文件不在了")
        try:
            body, content_type = _multipart("media", target.name, target.read_bytes())
            url = f"{UPLOAD_BASE}?key={self.key}&type=file"
            payload = self._post_form(url, body, content_type)
        except Exception as exc:
            return SinkResult(ok=False, code=-2, message=f"{type(exc).__name__}: {exc}")
        if int(payload.get("errcode") or 0) != 0:
            return SinkResult(ok=False, code=int(payload.get("errcode") or -1),
                              message=str(payload.get("errmsg") or "上传失败"))
        media_id = str(payload.get("media_id") or "")
        if not media_id:
            return SinkResult(ok=False, code=-1, message="上传后没拿到 media_id")
        return self._deliver({"msgtype": "file", "file": {"media_id": media_id}})

    # ------------------------------------------------------------------ 重试
    def _deliver(self, payload: dict[str, Any]) -> SinkResult:
        """统一发送入口：限速 + 重试 + 把错误码翻成人话。"""
        if not self.enabled:
            return SinkResult(ok=False, code=-1, message="群机器人推送未启用")
        if not self.key:
            return SinkResult(ok=False, code=-1, message="还没填 Webhook 地址")

        attempts = 0
        rate_retried = False
        last = SinkResult(ok=False, code=-1, message="未发送")
        while attempts <= self.retry_times + 1:
            attempts += 1
            self._throttle()
            try:
                data = self._post_json(payload)
            except urllib.error.HTTPError as exc:
                last = SinkResult(ok=False, code=exc.code,
                                  message=f"HTTP {exc.code}（检查网络或代理）")
                self.last_error = last.message
                self._sleep(min(1.5 * attempts, 5.0))
                continue
            except Exception as exc:
                last = SinkResult(ok=False, code=-2,
                                  message=f"{type(exc).__name__}: {exc}")
                self.last_error = last.message
                self._sleep(min(1.5 * attempts, 5.0))
                continue

            errcode = int(data.get("errcode") or 0)
            if errcode == 0:
                return SinkResult(ok=True, code=0, message="ok")

            errmsg = str(data.get("errmsg") or "")
            last = SinkResult(ok=False, code=errcode, message=errmsg)
            self.last_error = errmsg
            # 撞到频率限制：等一分钟再重试一次（只重试一次，不无限循环）
            if errcode == RATE_ERROR and not rate_retried:
                rate_retried = True
                self._sleep(min(self.rate_wait_seconds, 61.0))
                continue
            break
        return last

    def explain(self, result: SinkResult) -> str:
        """把错误码翻译成用户看得懂的一句话。"""
        if result.ok:
            return "发送成功"
        hint = BOT_ERROR_HINTS.get(result.code, "")
        return f"发送失败 errcode={result.code} {result.message}" + (f"（{hint}）" if hint else "")
