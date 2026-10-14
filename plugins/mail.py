"""邮箱插件：把邮件变成待办。

=====================================================================
它解决什么问题
=====================================================================
学校、公司、平台的通知一大半是发邮件 —— 尤其是"大人"那半边用户：
报税、账单、面试通知、快递、教务。这些邮件里往往明明白白写着
"请于 X 日前提交 / 回复 / 办理"，但它们躺在邮件里，没人会去翻。

拉一次，交给同一条理解管线（规则 + AI），该变待办的变待办。

=====================================================================
三条边界（都是故意的）
=====================================================================
1. **只在点「同步」时拉**（`run.py --sync` / 界面那个 ⟳）。**不轮询** ——
   插件性能预算里写着"闲时一行代码都不跑"，常驻 IMAP 连接既费内存又
   容易被邮箱服务商当成异常登录。
2. **不标已读**：用 `readonly=True` 打开邮箱，也不发 `\\Seen`。
   你手机上没读过的那封，同步完还是没读过 —— 这个工具不该替你动邮箱。
3. **失败要说人话**：连不上、密码错、被限流，一句一句告诉用户该改什么
   （最常见的就是把"登录密码"填进来了，其实要填**授权码**）。

=====================================================================
怎么用
=====================================================================
    [mail]
    enabled = true
    host = "imap.qq.com"       # QQ 邮箱；163 是 imap.163.com
    user = "你的邮箱地址"
    password = "授权码（不是登录密码！）"

然后跑一次 `python run.py --sync`，或点界面上的「⟳ 同步」。

各家的 host 和"授权码在哪拿"：
    QQ 邮箱    imap.qq.com     设置 → 账号 → POP3/IMAP…服务 → 生成授权码
    163 邮箱   imap.163.com    设置 → POP3/SMTP/IMAP → 开启 → 新增授权密码
    Gmail      imap.gmail.com  需要"应用专用密码"（开了两步验证才有）
    Outlook    outlook.office365.com

=====================================================================
想看这里的话
=====================================================================
  改"取多少封 / 看几天"      → config.toml 的 [mail] max_per_sync / keep_days
  改正文怎么从 HTML 里剥    → `_plain_text()`
  改来源名/发送人怎么显示    → `_to_message()`
  加附件（图片、PDF）        → 目前**不发附件**，只读正文；要加得先想清楚
                               附件怎么落到 data 目录（见 CONTRACT.md）
"""

from __future__ import annotations

import datetime as dt
import email
import email.header
import email.utils
import html
import imaplib
import re
from app.contracts.interfaces import Source
from app.contracts.models import Message
from app.runtime.config import config, default_data_dir

NAME = "mail"
TITLE = "邮箱"
CONFIG_SECTION = "mail"
DESCRIPTION = "把邮箱里的通知变成待办。只在点「同步」时拉一次，不常驻、不标已读。"

MARKER_NAME = "mail-last-uid.txt"
MAX_BODY_CHARS = 1500        # 一封邮件的正文最多取多少字（再长就切掉，避免淹列表）
MAX_MESSAGE_BYTES = 2 * 1024 * 1024      # 超过 2 MB 的邮件跳过（大附件正文没意义）
CONNECT_TIMEOUT = 20         # 连不上就快点失败，别让「同步」按钮转两分钟

_TAG = re.compile(r"<[^>]+>")
_SCRIPT_BLOCK = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_BLANK_LINES = re.compile(r"\n{3,}")


def _endpoint() -> tuple[str, int, str, str, str]:
    """把配置读出来。(host, port, user, password, folder)"""
    def get(key: str, default="") -> str:
        return str(config.get("mail", key, default) or "").strip()

    try:
        port = int(config.get("mail", "port", 993) or 993)
    except (TypeError, ValueError):
        port = 993
    return (get("host"), port, get("user"), get("password"),
            get("folder") or "INBOX")


def _limits() -> tuple[int, int]:
    """(最多几封, 只看最近几天)。配置写坏了就退回默认值，别抛异常。"""
    def number(key: str, default: int) -> int:
        try:
            value = int(config.get("mail", key, default) or default)
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    return number("max_per_sync", 10), number("keep_days", 7)


def _decode(raw) -> str:
    """邮件头（可能带编码字）→ 人看得懂的一行字。"""
    if raw is None:
        return ""
    text = str(raw)
    try:
        parts = email.header.decode_header(text)
    except Exception:                                  # noqa: BLE001
        return text.strip()
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(charset or "utf-8", "replace"))
        else:
            out.append(chunk)
    return " ".join("".join(out).split())


def _strip_html(body: str) -> str:
    """HTML 正文 → 纯文字。

    为什么要自己剥：装 beautifulsoup 只为了这个不值当（多一个依赖、多几 MB）；
    而邮件 HTML 里真正碍事的就三样：脚本、样式、标签本身。
    """
    text = _SCRIPT_BLOCK.sub(" ", body)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", text, flags=re.IGNORECASE)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES.sub("\n\n", text).strip()


def _plain_text(raw_bytes: bytes) -> str:
    """一封邮件的正文 → 纯文字（优先 text/plain，没有就把 HTML 剥了）。"""
    try:
        message = email.message_from_bytes(raw_bytes)
    except Exception:                                  # noqa: BLE001
        return ""
    plain: list[str] = []
    rich: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if (part.get("Content-Disposition") or "").startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            body = payload.decode(charset, "replace")
        except LookupError:                            # 认不出的字符集
            body = payload.decode("utf-8", "replace")
        if part.get_content_type() == "text/plain":
            plain.append(body)
        elif part.get_content_type() == "text/html":
            rich.append(_strip_html(body))
    text = "\n".join(plain).strip() or "\n".join(rich).strip()
    return _BLANK_LINES.sub("\n\n", text).strip()


def _to_message(uid: str, raw_bytes: bytes) -> Message:
    """一封邮件 → 标准消息。**不在这里判断"是不是待办"** —— 那是理解层的事。"""
    try:
        message = email.message_from_bytes(raw_bytes)
    except Exception:                                  # noqa: BLE001
        message = email.message.Message()
    subject = _decode(message.get("Subject"))
    sender = _decode(message.get("From"))
    sent_at = ""
    try:
        parsed = email.utils.parsedate_to_datetime(message.get("Date") or "")
        sent_at = parsed.strftime("%Y-%m-%d %H:%M:%S") if parsed else ""
    except Exception:                                  # noqa: BLE001
        sent_at = ""
    body = _plain_text(raw_bytes)[:MAX_BODY_CHARS]
    text = f"{subject}\n{body}".strip() if subject else body
    return Message(
        msg_id=f"mail:{uid}",
        source_kind="mail",
        # 来源名写"邮箱"而不是你的邮箱地址：推送里会显示来源，
        # 没必要把邮箱地址塞进每一条推送。
        source_name="邮箱",
        sender=sender,
        sent_at=sent_at or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        text=text,
        raw={"subject": subject, "from": sender, "uid": uid},
    )


def _marker() -> "object":
    from pathlib import Path

    return Path(default_data_dir()) / MARKER_NAME


def _last_uid() -> str:
    """上次拉到哪个 UID 了（存一个数，免得每次同步都把同一批邮件重跑一遍 AI）。"""
    try:
        return _marker().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _save_last_uid(uid: str) -> None:
    try:
        path = _marker()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(uid), encoding="utf-8")
    except OSError:
        pass                     # 存不下来只是下次多跑一遍，不该让同步失败


class MailSource(Source):
    """只拉不推的来源。什么时候拉由 `--sync` / 界面那个按钮决定。"""

    kind = "mail"
    title = TITLE

    def __init__(self) -> None:
        self.notes: list[str] = []

    def submit(self, message: Message) -> Message:
        return message           # 邮箱只有"拉"，没有"推"

    def fetch(self) -> list[Message]:
        self.notes = []
        if not config.get("mail", "enabled", False):
            return []
        host, port, user, password, folder = _endpoint()
        missing = [name for name, value in (("邮箱地址", user),
                                           ("授权码", password),
                                           ("服务器地址 host", host)) if not value]
        if missing:
            # 不猜、不静默：说清楚缺哪一项，去哪填
            self.notes.append(
                f"邮箱插件还差一项没填：{'、'.join(missing)} —— "
                "去「设置 → 收消息 → 邮箱」补上，再点一次同步。")
            return []
        limit, keep_days = _limits()
        since = (dt.datetime.now() - dt.timedelta(days=keep_days)).strftime("%d-%b-%Y")
        last_uid = _last_uid()

        conn = None
        try:
            conn = imaplib.IMAP4_SSL(host, port, timeout=CONNECT_TIMEOUT)
            conn.login(user, password)
            conn.select(folder, readonly=True)          # 不标已读
            # 有上次的位置就只要新的；第一次就用"最近 N 天"兜住
            criteria = ([f"UID {int(last_uid) + 1}:*"]
                        if last_uid.isdigit() else ["SINCE", since])
            status, data = conn.uid("SEARCH", None, *criteria)
            if status != "OK":
                self.notes.append(f"邮箱搜索没成：{status} —— 换个时间再试一次。")
                return []
            uids = [item.decode() for item in (data[0] or b"").split() if item]
        except imaplib.IMAP4.error as exc:
            self.notes.append(
                f"邮箱登录/连接失败：{exc} —— 多半是密码填成了**登录密码**，"
                "这里要填邮箱设置里生成的**授权码**（各家叫法不同）。")
            return []
        except Exception as exc:                        # noqa: BLE001
            self.notes.append(f"邮箱连不上：{type(exc).__name__}: {exc}")
            return []

        out: list[Message] = []
        try:
            for uid in uids[-limit:]:                   # 只取最新的几封
                raw = self._raw(conn, uid)
                if raw is None:
                    continue
                out.append(_to_message(uid, raw))
            if uids:
                _save_last_uid(uids[-1])                # 记到哪儿了
            if len(uids) > limit:
                self.notes.append(
                    f"邮箱里最近 {keep_days} 天有 {len(uids)} 封，这次只取了最新的 "
                    f"{limit} 封 —— 想一次多收点就改 [mail] max_per_sync。")
        finally:
            try:
                conn.logout()
            except Exception:                           # noqa: BLE001
                pass
        return out

    @staticmethod
    def _raw(conn, uid: str) -> bytes | None:
        """取一封邮件的原文；太大就跳过（不然一封 30 MB 的邮件能把内存吃光）。"""
        status, data = conn.uid("FETCH", uid, "(RFC822.SIZE)")
        if status == "OK" and data and isinstance(data[0], bytes):
            match = re.search(rb"RFC822\.SIZE (\d+)", data[0])
            if match and int(match.group(1)) > MAX_MESSAGE_BYTES:
                return None
        status, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if status != "OK":
            return None
        for item in data or []:
            if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
                return item[1]
        return None


def register(registry) -> None:
    registry.add_source(MailSource())
