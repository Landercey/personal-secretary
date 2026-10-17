"""试一下 AI 通不通：填完地址/模型/密钥点一下就知道，不用等一条长消息。

为什么不复用插件里的 `call_model`：那是**真实调用**（prompt 是整条消息、
不带 max_tokens）。这里只发一句"只回两个字：收到"、限制输出长度 ——
测试用的请求要最便宜、最快，而且**不需要**先打开"允许上传"
（发出去的不是用户内容，是一句固定的话）。

地址规则和插件保持一致（`plugins/ai.py` 的 `_endpoint`）：填到 /v1 或
直接填到 /chat/completions 都认。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .config import config

PROMPT = "只回两个字：收到"


def _url() -> str:
    base = str(config.get("ai", "base_url", "") or "").strip().rstrip("/")
    if not base:
        return ""
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def probe(timeout: float = 20.0) -> dict:
    """打一次最便宜的请求。返回 {"ok", "message", "model", "ms", "reply"}。"""
    url = _url()
    model = str(config.get("ai", "model", "") or "").strip()
    if not url:
        return {"ok": False, "message": "没填接口地址（设置 → AI 增强 → 接口地址）"}
    if not model:
        return {"ok": False, "message": "没填模型名（照服务商那边写的抄一个）"}

    key = str(config.get("ai", "api_key", "") or "").strip() or "none"
    payload = {"model": model,
               "messages": [{"role": "user", "content": PROMPT}],
               "temperature": 0,
               # 只让它回几个字：测试不该花掉一次完整回答的钱
               "max_tokens": 16}
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}",
                 "User-Agent": "personal-secretary/ai-test"},
        method="POST")

    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:160]
        return {"ok": False, "model": model,
                "message": f"接口回了 HTTP {exc.code}：{detail or '（没有内容）'}"}
    except Exception as exc:                    # 连不上 / 超时 / 地址写错
        return {"ok": False, "model": model,
                "message": f"连不上：{type(exc).__name__}: {exc}"}

    reply = str(((body.get("choices") or [{}])[0].get("message") or {})
                .get("content") or "").strip()
    spent = round((time.monotonic() - started) * 1000)
    message = f"通了（{spent} ms）：{model} 回了「{reply[:20] or '（空）'}」"
    if not (config.get("ai", "enabled") and config.get("ai", "allow_upload")):
        # 通了 ≠ 会用它：两个开关是分开的，这里顺手点出来，省得他以为坏了
        message += "。不过开关还没全开 —— 把「启用 AI」和「允许上传」都打开才会真的用。"
    return {"ok": True, "message": message, "model": model, "ms": spent, "reply": reply}
