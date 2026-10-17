"""通用推送地址：钉钉 / 飞书 / Bark —— 只填一个地址就行。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  加一家的支持            → `VENDORS` 那张表（认域名）+ 下面挨着写一个 `_xxx()`
  改某家的报文格式        → 对应的 `_dingtalk()` / `_feishu()` / `_bark()`（老的 `_pushplus()` 也还在）
  认不出来的地址怎么办    → `_generic()`（按 {title, text} 发，自建服务够用）
  改"算不算发送成功"      → `_verdict()`（**四家失败时也返回 200**，只能看 body 里的错误码）
  改超时/重试             → config.toml 的 [push_webhook] timeout / retry_times

这家的接口有两个反直觉的点：状态码不可信（见 `_verdict`）、
钉钉还必须让正文里带上用户设的关键词，不然直接拒收。

=====================================================================
为什么加这个（2026-10-12）
=====================================================================
企业微信得先注册一个"企业"（哪怕只有你一个人）才能建群机器人，
这一步劝退的人比想象里多。而钉钉、飞书建个群就有机器人地址 ——
都不用任何"企业"身份。

=====================================================================
怎么认的
=====================================================================
只看地址里的域名（`VENDORS` 那张表）。用户只管把整条地址粘进来，
别的什么都不用填。表里没有的域名按最普通的 `{"title":..,"text":..}`
发出去 —— 自建的服务和以后新增的通道都能凑合用。

四家的报文格式照官方文档写。**每一家失败时也返回 HTTP 200**，
所以不能只看状态码，得看 body 里的错误码（见 `_verdict`）：

  钉钉      https://open.dingtalk.com/document/orgapp/custom-robot-access
  飞书      https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
  Bark      https://bark.day.app

=====================================================================
三条边界（都是故意的）
=====================================================================
1. **不发附件**：这四家都没有"发文件"的接口（企业微信有）。要用附件还是
   走企业微信 —— 这里发不了会明说，不会让用户以为附件也过去了。
2. **不限速**：一天就推两三条，限速没意义（企业微信那条有官方硬限制才需要）。
3. **不切分、不截断**：这些通道的正文上限（钉钉 20 KB）比企业微信的
   4096 字节宽得多，一天的汇总塞得下；真超了目标会报错，错误会显示出来。

shortcut: 钉钉的「加签」安全设置不支持（要拿密钥算签名）。装机器人时安全设置
          请选「自定义关键词」、关键词填「待办」——报文里固定带了这两个字。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlsplit

from ..contracts.interfaces import Sink, SinkResult
from ..contracts.models import PushPayload

# 钉钉机器人选了「自定义关键词」安全设置时，报文里必须出现那个词，
# 否则直接被拒（errcode 310000）。所以固定带上这两个字，
# 文档里也让用户把关键词填成同一个词 —— 两边对上才不会被拒。
DINGTALK_KEYWORD = "待办"

# 造报文返回 (请求体, 判断成败的字段, 算成功的值)；字段为空 = HTTP 200 就算成功。
Build = Callable[[str, str], tuple[dict[str, Any], str, tuple[str, ...]]]


def _dingtalk(title: str, body: str) -> tuple[dict[str, Any], str, tuple[str, ...]]:
    head = f"【{DINGTALK_KEYWORD}】{title}" if title else f"【{DINGTALK_KEYWORD}】"
    # 钉钉的纯文本消息只有一个正文框，标题得自己拼进去。
    return {"msgtype": "text", "text": {"content": f"{head}\n{body}"}}, "errcode", ("0",)


def _feishu(title: str, body: str) -> tuple[dict[str, Any], str, tuple[str, ...]]:
    text = f"{title}\n{body}" if title else body
    return {"msg_type": "text", "content": {"text": text}}, "code", ("0",)


def _pushplus(title: str, body: str) -> tuple[dict[str, Any], str, tuple[str, ...]]:
    # ⚠️ 这家后来**收费**了，我们不再推荐（文档里已经不提它）。
    # 代码留着只是因为：老用户的 config.toml 里可能已经填着它的地址，
    # 删掉这段会让那份配置直接发不出去 —— 属于"偷偷把人弄坏"。
    # token 跟在地址后面（?token=xxx）就行，所以这里不用再带。
    # 用 txt 模板：默认的 html 模板会把换行吃掉，一天的汇总就糊成一坨了。
    return {"title": title, "content": body, "template": "txt"}, "code", ("200",)


def _bark(title: str, body: str) -> tuple[dict[str, Any], str, tuple[str, ...]]:
    return {"title": title, "body": body}, "code", ("200",)


def _wecom(title: str, body: str) -> tuple[dict[str, Any], str, tuple[str, ...]]:
    """企业微信群机器人。

    本来有单独的「企业微信机器人」配置段，但很多人手里先拿到的是那条 webhook 地址，
    会顺手粘进通用地址里 —— 以前会走"自定义地址"，发出去的报文形状不对，
    企业微信直接拒（errcode 93000），用户只看到"推送失败"。
    """
    # 企业微信的 text 消息上限 2048 字节（一个汉字 3 字节），超了整条被拒。
    # 专用通道早就按这个截断了，这里也得截 —— 不然一部长一点的汇总就发不出去。
    from .wecom_bot import TEXT_LIMIT, fit_bytes

    text = fit_bytes(f"{title}\n{body}" if title else body, TEXT_LIMIT)
    return {"msgtype": "text", "text": {"content": text}}, "errcode", ("0",)


def _generic(title: str, body: str) -> tuple[dict[str, Any], str, tuple[str, ...]]:
    """认不出的地址：发一个最普通的形状，不猜成败（HTTP 200 就算发到了）。"""
    return {"title": title, "text": body}, "", ()


VENDORS: tuple[tuple[str, str, Build], ...] = (
    ("qyapi.weixin.qq.com", "企业微信机器人", _wecom),
    ("dingtalk.com", "钉钉机器人", _dingtalk),
    ("feishu.cn", "飞书机器人", _feishu),
    ("pushplus.plus", "PushPlus（收费，不再推荐）", _pushplus),
    ("day.app", "Bark", _bark),
    ("", "自定义地址", _generic),
)


def vendor_of(url: str) -> tuple[str, Build]:
    """地址属于哪一家。都不认就是"自定义地址"（最后那条兜底）。"""
    host = (urlsplit(str(url or "")).hostname or "").lower()
    for needle, label, build in VENDORS:
        if needle and needle in host:
            return label, build
    return VENDORS[-1][1], VENDORS[-1][2]


def _verdict(body: str, field: str, good: tuple[str, ...]) -> tuple[bool, int, str]:
    """看返回的 body，判断这一条到底发出去没有。

    **不能只看 HTTP 200** —— 这四家失败时也回 200，靠 body 里的错误码区分。
    "以为发出去了、其实没发"是最难查的一类故障，所以这里宁可啰嗦。
    """
    try:
        data = json.loads(body)
    except ValueError:
        # 认不出的地址不猜：回 200 且能收到东西，就算发到了。
        if not field:
            return True, 0, "ok"
        return False, -2, f"返回的不是 JSON：{body[:80]}"
    if not isinstance(data, dict) or not field:
        return True, 0, "ok"

    value = str(data.get(field, "")).strip()
    if value in good:
        return True, 0, "ok"
    # 各家的错误说明字段名不一样，挨个看一眼，用户能看懂就行
    hint = ""
    for name in ("errmsg", "msg", "message", "error"):
        hint = str(data.get(name) or "").strip()
        if hint:
            break
    try:
        code = int(value or -1)
    except ValueError:
        code = -1
    return False, code, f"{field}={value or '空'} {hint}".strip()


class WebhookSink(Sink):
    """通用推送地址。实现契约里的 Sink 接口，所以能被随时换掉。"""

    name = "webhook"

    def __init__(self, url: str = "", timeout: float = 10.0, retry_times: int = 2,
                 enabled: bool = True, sleep: Callable[[float], None] = time.sleep) -> None:
        self.url = str(url or "").strip()
        self.timeout = float(timeout or 10)
        self.retry_times = max(0, int(retry_times or 0))
        self.enabled = bool(enabled)
        self.last_error = ""
        # 只为了测试：测试里换成假的 sleep，就不用真的等。
        self._sleep = sleep

    @classmethod
    def from_config(cls) -> "WebhookSink":
        """按 config.toml 里的 [push_webhook] 建一个通道。

        ⚠️ `config` 要**在函数里**导入（和 wecom_bot 同理）：写在文件顶上
        会在 import 那一刻绑死成当时那个对象，之后换了配置它还是看旧的。
        """
        from ..runtime.config import config

        return cls(
            url=str(config.get("push_webhook", "url", "") or ""),
            timeout=float(config.get("push_webhook", "timeout", 10) or 10),
            retry_times=int(config.get("push_webhook", "retry_times", 2) or 0),
            enabled=bool(config.get("push_webhook", "enabled", False)),
        )

    def ready(self) -> bool:
        """能发了吗？—— 开着 + 填了地址。"""
        return self.enabled and bool(self.url)

    def describe(self) -> str:
        """一句话说明当前状态，给设置界面和自检用。

        ⚠️ **只报域名，绝不报整条地址** —— 地址里带着 token，
        而这段文字会出现在用户复制粘贴到公开场合的意见反馈包里。
        """
        if not self.enabled:
            return "通用推送地址：未启用"
        if not self.url:
            return "通用推送地址：还没填地址"
        label, _ = vendor_of(self.url)
        return f"通用推送地址：已启用｜{label}"

    # ------------------------------------------------------------------ 发送
    def deliver(self, payload: PushPayload) -> SinkResult:
        """契约入口：把一条 PushPayload 送出去（只发正文，附件不支持）。"""
        if not self.ready():
            reason = "通用推送地址未启用" if not self.enabled else "还没填推送地址"
            return SinkResult(ok=False, code=-1, message=reason)

        title = str(payload.title or "")
        body = str(payload.body or payload.title or "")
        _, build = vendor_of(self.url)
        request_body, field, good = build(title, body)
        result = self._post(request_body, field, good)
        if result.ok and payload.attachments:
            names = "、".join(item.name for item in payload.attachments)
            result = SinkResult(ok=True, code=0,
                                message=f"正文已发；附件没发（这个通道不支持发文件）：{names}")
        return result

    def _post(self, request_body: dict[str, Any], field: str,
              good: tuple[str, ...]) -> SinkResult:
        data = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=data, headers={"Content-Type": "application/json"})
        last = SinkResult(ok=False, code=-1, message="未发送")
        for attempt in range(self.retry_times + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    text = response.read(200_000).decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                # 4xx/5xx 时对方一般会在 body 里写原因（"token 无效"这种），
                # 只报个状态码用户没法查，也查不动。带上前 120 个字。
                detail = ""
                try:
                    detail = exc.read(400).decode("utf-8", "replace").strip()
                except Exception:                      # 读不到就算了，别把报错盖住
                    pass
                last = SinkResult(
                    ok=False, code=exc.code,
                    message=f"HTTP {exc.code}：{detail[:120]}" if detail
                    else f"HTTP {exc.code}（地址填对了吗）")
            except Exception as exc:
                last = SinkResult(ok=False, code=-2,
                                  message=f"{type(exc).__name__}: {exc}")
            else:
                ok, code, message = _verdict(text, field, good)
                if ok:
                    return SinkResult(ok=True, code=0, message="ok")
                last = SinkResult(ok=False, code=code, message=message)
            self.last_error = last.message
            if attempt < self.retry_times:
                self._sleep(min(1.5 * (attempt + 1), 5.0))
        return last
