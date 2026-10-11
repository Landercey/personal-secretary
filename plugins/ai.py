"""AI 增强插件：把消息交给一个 OpenAI 兼容接口，挑出"明确要做的事"。

=====================================================================
它解决什么问题
=====================================================================
规则解析擅长短消息（"明天下午三点交作业"），但**长通知**会露馅：

    各位同学，本学期《军事理论》课程考核安排如下：一、12 月 20 日前
    提交课程论文，字数不少于 3000 字，交到学习通作业区；二、12 月 27 日
    上午 9:00-11:00 随堂测验，地点由任课老师通知……

规则会切出"字数不少于 3000"这种碎片（真实案例，用户报过）。AI 能把这种
长文归纳成"交《军事理论》论文（12-20 前）"+"随堂测验（12-27 09:00）"。

=====================================================================
三条红线（都写死在代码里，不是可选项）
=====================================================================
1. **默认关闭，而且要开两道闸**：`enabled` + `allow_upload` 都为 true 才会
   真的往外发。消息里有群名、有原文，开源工具不能替用户决定"要不要上传"。
2. **它挂了，程序照常**：连不上、超时、返回一堆废话 —— 一律原样交回规则
   解析的结果，绝不抛异常（管线里虽然有 try/except，但在这里兜住才能
   把"为什么没用上 AI"记下来）。
3. **不猜时间**：模型给的时间戳解析不出来就当没写时间。宁可少一条时间，
   也不要一条错的时间 —— 错时间会让用户错过真正的事。

=====================================================================
装法 / 配置
=====================================================================
这个文件本来就在 `plugins/` 里，不用装。去「设置 → 看懂消息 → AI 增强」填：

    enabled       = true            # 总开关
    allow_upload  = true            # 允许把内容发出去（第二道闸）
    base_url      = "http://127.0.0.1:11434/v1"   # 本地 Ollama
    model         = "qwen2.5:7b"

云端各家（硅基流动、DeepSeek、OpenAI…）形状一样，地址填到 /v1 为止。

=====================================================================
想改这里的话
=====================================================================
  改提示词（要它抽得更狠/更保守）  → _SYSTEM / _AGGRESSIVE
  改什么消息才送 AI                → should_ask()
  换接口形状（不是 OpenAI 兼容的）  → call_model()
  不想用它                        → 把 ai.enabled 关掉，或删掉这个文件
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.request

from app.contracts.interfaces import DraftEnricher
from app.contracts.models import TaskDraft
from app.runtime.config import config, default_data_dir

NAME = "ai"
TITLE = "AI 增强"
# 我的设置在设置面板的哪一段 —— 界面靠它给一个「去设置」按钮。
CONFIG_SECTION = "ai"
DESCRIPTION = "把长通知、表格、长文档交给 AI（云端或本地都行）挑出要做的事。默认关闭。"

ORDER = 90          # 规则解析是 10/20，AI 排在最后（先有草稿，再让它加工）
# 我们给 AI 出的草稿打这个可信度：小于 1 就是"这不是规则算出来的，是模型说的"，
# 推送那边靠它标【AI 整理，可能有误】。
AI_CONFIDENCE = 0.6
MAX_INPUT_CHARS = 4000      # 再长的正文只送前这么多字，不然费钱又慢

_SYSTEM = """你是一个待办抽取器。只做一件事：从用户给的中文内容里挑出「明确写了要做的事」。

规则：
1. 只挑原文明确写了要做的事。原文没写就不要。不要凭常识补，不要脑补。
2. 时间只在原文写了的时候才填，格式 "YYYY-MM-DD HH:MM"：
   - 原文说"明天/后天"就按今天的日期算出来；
   - 只说了哪一天、没说几点，填那天的 23:59；
   - 算不出来、拿不准，填 null。不要猜。
   今天的日期是 {today}。
3. 不要把闲聊、吐槽、纯提醒（比如"记得带伞"这类不是任务的）当成待办。
4. 每条给：title（不超过 20 字，动词开头）、due（或 null）、location（或 null）。
5. 一条都没有，返回空数组 []。

只输出 JSON，形如 [{{"title":"交军事理论论文","due":"2026-12-20 23:59","location":"学习通"}}]。
不要解释、不要代码块、不要多余的字。"""

_AGGRESSIVE = """
6. 激进模式：允许把一大段通知归纳成一两条要点；原文信息足够时可以补出合理的时间。"""


def _endpoint() -> str:
    """把用户填的地址补成完整的 chat/completions。"""
    base = str(config.get("ai", "base_url", "") or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _today() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d")


def _parse_due(text) -> str | None:
    """模型给的时间变成我们库里那个格式。解析不出来就 None（**不猜**）。"""
    raw = str(text or "").strip()
    if not raw or raw.lower() in {"null", "none", "未知"}:
        return None
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                    "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            parsed = dt.datetime.strptime(raw, pattern)
        except ValueError:
            continue
        if "%H" in pattern:
            return parsed.strftime("%Y-%m-%d %H:%M:00")
        return parsed.strftime("%Y-%m-%d 23:59:00")
    return None


def _json_array(text: str) -> list:
    """模型的回答变成列表。它会不听劝地加上 ```json 或者前后说两句，都得认。"""
    body = str(text or "").strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body[:4].lower() == "json":
            body = body[4:]
    start, end = body.find("["), body.rfind("]")
    if start < 0 or end < start:
        return []
    try:
        data = json.loads(body[start:end + 1])
    except ValueError:
        return []
    return [item for item in data if isinstance(item, dict)]


def _usage_file():
    return default_data_dir() / "ai-usage.json"


def used_today() -> int:
    """今天已经用掉多少 token（模型自己报的数）。读不出来就当 0。"""
    try:
        data = json.loads(_usage_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if str(data.get("date") or "") != _today():
        return 0
    return int(data.get("tokens") or 0)


def add_usage(tokens: int) -> None:
    if not tokens:
        return
    total = used_today() + int(tokens)
    path = _usage_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"date": _today(), "tokens": total},
                                   ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def budget_left() -> bool:
    limit = int(config.get("ai", "token_budget_per_day", 0) or 0)
    return limit <= 0 or used_today() < limit


def note_problem(text: str) -> None:
    """把"AI 这次没用上"记下来，界面/自检能读，用户不用猜。"""
    try:
        path = default_data_dir() / "ai-status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"ok": not text, "text": text,
             "at": dt.datetime.now().isoformat(" ", "seconds")},
            ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def call_model(text: str) -> str:
    """问一次模型，返回它说的话（原始字符串）。失败抛异常，由调用方兜。"""
    payload = {
        "model": str(config.get("ai", "model", "") or "").strip(),
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": text[:MAX_INPUT_CHARS]},
        ],
        "temperature": 0,
        "stream": False,
    }
    key = str(config.get("ai", "api_key", "") or "").strip() or "none"
    request = urllib.request.Request(
        _endpoint(), data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}",
                 "User-Agent": "personal-secretary/ai"},
        method="POST")
    timeout = float(config.get("ai", "timeout_seconds", 60) or 60)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8", "replace"))
    add_usage(int((body.get("usage") or {}).get("total_tokens") or 0))
    choices = body.get("choices") or [{}]
    return str((choices[0].get("message") or {}).get("content") or "")


def _system_prompt() -> str:
    prompt = _SYSTEM.format(today=_today())
    if str(config.get("ai", "mode", "conservative") or "").strip() == "aggressive":
        prompt += _AGGRESSIVE
    return prompt


def should_ask(text: str) -> bool:
    """什么样的消息值得问 AI。短消息规则解析够用，AI 又慢又花钱 —— 不送。"""
    limit = int(config.get("ai", "min_chars", 80) or 80)
    return len(" ".join(str(text or "").split())) >= limit


class AiEnricher(DraftEnricher):
    """草稿级零件：把长消息交给 AI，它挑出来的就替换掉规则的结果。"""

    name = "ai"
    order = ORDER

    def enabled(self) -> bool:
        # 两道闸都要开：想用 + 允许上传。少一个就安静地跳过。
        if not config.get("ai", "enabled", False):
            return False
        if not config.get("ai", "allow_upload", False):
            return False
        if not _endpoint() or not str(config.get("ai", "model", "") or "").strip():
            return False
        return budget_left()

    def process(self, message, drafts):
        # 再查一遍开关。管线本来会先问 enabled()，但"要不要把用户内容发出去"
        # 这种事不该指望调用方守规矩 —— 谁把它接错了都不该造成上传。
        if not self.enabled():
            return drafts
        text = str(getattr(message, "text", "") or "")
        if not should_ask(text):
            return drafts
        try:
            answer = call_model(text)
        except Exception as exc:
            # 退回规则解析 —— 而且要把原因记下来："AI 连不上了"必须能被看见，
            # 不能变成"我这条怎么没收到"。
            why = f"{type(exc).__name__}: {exc}"
            message.raw.setdefault("ai_error", why)
            note_problem(f"AI 用不了，这次走了规则解析：{why}")
            return drafts or [self._fallback(message, text)]

        items = _json_array(answer)
        if not items:
            # 模型说"这里面没有明确要做的事"。保守起见**保留规则的结果**：
            # 它漏了会漏掉真待办，那比多一条更糟。
            note_problem("")
            return drafts

        quotes = int(config.get("push", "quote_max_chars", 80) or 80)
        title_max = int(config.get("title", "max_len", 200) or 200)
        made: list[TaskDraft] = []
        for item in items:
            title = " ".join(str(item.get("title") or "").split())
            if not title:
                continue
            made.append(TaskDraft(
                title=title[:title_max],
                detail=text[:600],
                due_at=_parse_due(item.get("due")),
                location=(" ".join(str(item.get("location") or "").split())
                          or None),
                source_kind=message.source_kind,
                source_name=message.source_name,
                sender=message.sender,
                origin_msg_id=message.msg_id,
                attachments=list(message.attachments),
                confidence=AI_CONFIDENCE,
                quoted=message.quote(quotes),
            ))
        note_problem("")
        return made or drafts

    def _fallback(self, message, text: str) -> TaskDraft:
        """AI 用不了、规则又一条都没产出时的**保底**。

        契约里写着"不能让 AI 出错变成什么都没收到"：一条长通知既没被规则读懂、
        AI 又没连上，那就把原文前 [push] fallback_text_chars 个字摆成一条，
        用户至少知道"有这么一条没读成"，而不是彻底丢失。

        故意**不**打【AI】标：这不是 AI 整理的，是原文照抄。
        """
        limit = int(config.get("push", "fallback_text_chars", 300) or 300)
        body = " ".join(str(text or "").split())
        quotes = int(config.get("push", "quote_max_chars", 80) or 80)
        return TaskDraft(
            title=f"看一眼这条（{len(body)} 字，AI 没连上）",
            detail=body[:limit],
            source_kind=message.source_kind,
            source_name=message.source_name,
            sender=message.sender,
            origin_msg_id=message.msg_id,
            attachments=list(message.attachments),
            quoted=message.quote(quotes),
        )


def register(registry) -> None:
    registry.add_draft(AiEnricher())
