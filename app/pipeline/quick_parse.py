"""规则版文本解析（不使用 AI，零 Token、零联网）。

分工原则（旧项目实测结论）：**时间交给规则，语义交给模型**。
  规则算日期比 7B 模型准得多（实测 4/5 对 3/7），模型只该用来判断
  "哪句才是待办"和"这一大段在讲什么"。

本文件是第 2 步的版本：从旧项目搬来经过验证的规则，并修掉一个已知根因
（见 `_build_title` 里的说明）。第 6 步会在此基础上补：排班表整体识别、
用户名字高亮、三档过滤强度、60 秒合并窗口。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from ..contracts.models import SOURCE_MANUAL, TaskDraft
from ..runtime.config import config
from .text_norm import to_simplified

_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
            "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}

_BULLET = re.compile(r"^\s*(?:[-*•·>#]|\d+\s*[.、)．，,]|[（(]\d+[）)])\s*")
_URL = re.compile(r"https?://\S+")
# 聊天里的占位符不是内容，标题里要去掉。
# 第二段是 QQ 协议的原始代码（[CQ:at,qq=123] / [CQ:flashtransfer,...]）——
# 旧库 170 条里就有一条待办的标题是一整串 CQ 代码，手机上看就是天书。
_PLACEHOLDER = re.compile(
    r"\[CQ:[^\]]*\]|\[(?:回复|图片|表情|文件[^\]]*)\]|@(?:我|全体|某人)"
)

# ---- 日期 ----
_DATE_YMD = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DATE_MD = re.compile(r"(\d{1,2})月(\d{1,2})[日号]")
_DATE_THIS_MONTH = re.compile(r"本月\s*(\d{1,2})[日号]")
_DATE_NEXT_MONTH = re.compile(r"下月\s*(\d{1,2})[日号]")
_DATE_REL = re.compile(r"(大后天|后天|今天|今晚|今早|明天|明晚|明早)")
_DATE_WEEK = re.compile(r"(下周|本周|这周|周|星期)([一二三四五六日天1-7])")

# ---- 时刻 ----
_TIME_HM = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")
_TIME_PERIOD = r"(上午|下午|中午|晚上|晚|早上|早晨|凌晨|今晚|明晚|夜里|夜间)?"
_TIME_CN = re.compile(rf"{_TIME_PERIOD}\s*(\d{{1,2}})\s*[点時时]\s*(半|\d{{1,2}}\s*分)?")

# ---- 中文数字的时刻：「上午十点」「两点半」「下午三点二十」----
# 手机上手写经常用中文数字，不转成阿拉伯数字会被当成"没有时间"。
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_NUM = r"[零一二两三四五六七八九十]{1,3}"
_CN_CLOCK = re.compile(rf"({_CN_NUM})\s*([点時时])")
_CN_MINUTE = re.compile(rf"({_CN_NUM})\s*分")
_CN_CLOCK_MIN = re.compile(rf"({_CN_NUM})\s*([点時时])\s*({_CN_NUM})(?!\s*分)")

# ---- 标题清理用（把所有时间片段摘掉）----
_DATE_STRIP = re.compile(
    r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}[日号]|本月\d{1,2}[日号]|下月\d{1,2}[日号]"
    r"|大后天|后天|今天|明天|今晚|明晚|今早|明早"
    r"|(?:下周|本周|这周|周|星期)[一二三四五六日天1-7])(?:以前|之前|前|后)?"
)
_TIME_STRIP = re.compile(
    rf"(?:{_TIME_PERIOD}\s*\d{{1,2}}\s*[:：]\s*\d{{2}}"
    rf"|{_TIME_PERIOD}\s*\d{{1,2}}\s*[点時时]\s*(?:半|\d{{1,2}}\s*分)?)"
    r"(?:以前|之前|前|后)?"
)

# ---- 地点 ----
_PLACE_KEY = (r"(?:楼|室|馆|厅|场|园|院|站|中心|食堂|教室|会议室|宿舍|图书馆|操场"
              r"|门口|大门|体育馆)")
_PLACE_SUFFIX = r"(?:门口|门外|里面|外面|附近|旁边|楼上|楼下|前面|后面)?"
_LOC_TAG = re.compile(r"(?:地点|地址)\s*[:：]\s*([^\s,，。;；]{2,20})")
_LOC_PLACE = re.compile(
    rf"在\s*([\u4e00-\u9fffA-Za-z0-9]{{0,8}}?{_PLACE_KEY}{_PLACE_SUFFIX}[0-9A-Za-z]{{0,6}})"
)
_LOC_VERB = re.compile(r"在\s*([^\s,，。;；]{2,12}?)(?:集合|开会|上课|考试|面试)")
# 兜底：OCR 常常把"在"漏掉（"图书馆门口集合"），直接按场所关键词匹配
_LOC_BARE = re.compile(rf"{_PLACE_KEY}{_PLACE_SUFFIX}[0-9A-Za-z]{{0,6}}")
_LOC_CHAR = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
# 向左扩展时的边界：碰到这些字就停（避免把"周六去""10点"吞进地点）
_LOC_STOP_CHARS = set("0123456789点月日号年周星期上午下午晚上早中去的在到把和与及给从")
_LOC_LEFT_LIMIT = 4
_BARE_LOC_BLACKLIST = ("页", "邮箱")

# ---- 程度词 ----
_URGENT = ("紧急", "立刻", "马上", "赶紧", "尽快", "加急")
_MUST = ("务必", "必须", "一定", "别忘", "不要忘")
_IMPORTANT = ("重要", "重点", "优先")
_ACK = ("确认", "回复", "回执", "接龙")

# ---- 噪声（标题、寒暄、时间戳）----
_NOISE_PATTERNS = (
    re.compile(r"^[\d\s\-:：./年月日号()（）]+$"),           # 纯时间戳
    re.compile(r"^[（(【\[]?\d{1,2}[.、)）】\]]?\s*$"),        # 纯编号
    re.compile(r"^[-=—_·*~#\s]+$"),                          # 分隔线
    re.compile(r"^(?:\[(?:图片|表情|回复|文件[^\]]*)\]|@我|@全体|@某人|\s)+$"),  # 纯占位符
)
_NOISE_WORDS = ("通知", "公告", "说明", "安排", "如下", "各位", "大家", "同学", "主题")
_NOISE_MAX_LEN = 24

_STRIP_ALL = re.compile(r"[\s,，。;；:：!！?？\-—_/\\|()（）\[\]【】\"'“”‘’]+")

# 通知里的"字段名"：光一个字段名 + 一个时间/地点，本身不是一件事。
# 实测踩过的：接龙里的"截止时间：10月3日 07:41"被记成待办「截止时间」。
# 这些词后面跟冒号就是"表单栏名"，不是一件事。
# 用后缀匹配而不是穷举全称：旧项目就是穷举漏掉了"集中办理时间"这种。
_FIELD_NOUNS = ("时间|地点|名单|姓名|学号|群号|费用|方式|要求|说明|备注|"
                "链接|网址|格式|内容|主题|日期|事项|电话|手机|邮箱|账号|"
                "联系人|联系方式|联系电话|对象|范围|编号")
_FIELD_LINE = re.compile(r"^\s*[^：:]{0,6}(?:" + _FIELD_NOUNS + r")\s*[：:]")
_FIELD_BARE = re.compile(r"(?:" + _FIELD_NOUNS + r")$")

# 长到这个份上就是"一整段话"：行数够多、且大半的行压根不像一件事
PROSE_MIN_LINES = 6


# ======================================================================
# 中文数字时刻 → 阿拉伯数字
# ======================================================================
def _cn_to_int(text: str) -> int | None:
    """「十」→10、「十一」→11、「二十三」→23；认不出来返回 None。"""
    total, current = 0, 0
    for char in text or "":
        if char in _CN_DIGIT:
            current = _CN_DIGIT[char]
        elif char == "十":
            total += (current or 1) * 10
            current = 0
        else:
            return None
    value = total + current
    return value if (total or current) else None


def cn_time_to_digits(text: str) -> str:
    """把「上午十点」「两点半」换成数字，后面所有规则共用一套。"""
    def clock(match: re.Match) -> str:
        value = _cn_to_int(match.group(1))
        return f"{value}点" if value is not None else match.group(0)

    def minute(match: re.Match) -> str:
        value = _cn_to_int(match.group(1))
        return f"{value}分" if value is not None else match.group(0)

    def clock_minute(match: re.Match) -> str:
        hour = _cn_to_int(match.group(1))
        minute_value = _cn_to_int(match.group(3))
        if hour is None or minute_value is None:
            return match.group(0)
        return f"{hour}点{minute_value}分"

    normalized = _CN_CLOCK_MIN.sub(clock_minute, text or "")
    return _CN_MINUTE.sub(minute, _CN_CLOCK.sub(clock, normalized))


# ======================================================================
# 日期 / 时刻 / 地点
# ======================================================================
def resolve_date(text: str) -> tuple[datetime | None, str | None]:
    """返回 (日期, 命中的原文)，取最早出现的那个。"""
    today = datetime.now()
    best: tuple[int, datetime, str] | None = None

    def consider(pos: int, dt: datetime, raw: str) -> None:
        nonlocal best
        if best is None or pos < best[0]:
            best = (pos, dt, raw)

    m = _DATE_YMD.search(text)
    if m:
        try:
            consider(m.start(), datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))),
                     m.group(0))
        except ValueError:
            pass

    m = _DATE_MD.search(text)
    if m:
        try:
            consider(m.start(), today.replace(month=int(m.group(1)), day=int(m.group(2))),
                     m.group(0))
        except ValueError:
            pass

    for pattern, month_offset in ((_DATE_THIS_MONTH, 0), (_DATE_NEXT_MONTH, 1)):
        m = pattern.search(text)
        if m:
            try:
                month = today.month + month_offset
                year = today.year + (month - 1) // 12
                month = (month - 1) % 12 + 1
                consider(m.start(), datetime(year, month, int(m.group(1))), m.group(0))
            except ValueError:
                pass

    m = _DATE_REL.search(text)
    if m:
        offset = {"今天": 0, "今晚": 0, "今早": 0, "明天": 1, "明晚": 1, "明早": 1,
                  "后天": 2, "大后天": 3}[m.group(1)]
        consider(m.start(), today + timedelta(days=offset), m.group(0))

    m = _DATE_WEEK.search(text)
    if m:
        prefix, day_char = m.group(1), m.group(2)
        target = _WEEKDAY.get(day_char)
        if target is not None:
            monday = today - timedelta(days=today.weekday())
            if prefix == "下周":
                dt = monday + timedelta(days=7 + target)      # 下一个自然周的周X
            elif prefix in ("本周", "这周"):
                dt = monday + timedelta(days=target)
            else:
                # 裸"周X"：取最近的那个。今天就是周X的话算下周
                # （"周五交"在周五一早说出口，指的通常是下周五）。
                delta = (target - today.weekday()) % 7
                if delta == 0:
                    delta = 7
                dt = today + timedelta(days=delta)
            consider(m.start(), dt, m.group(0))

    if best is None:
        return None, None
    return best[1], best[2]


def resolve_time(text: str) -> tuple[int, int, str] | None:
    """返回 (时, 分, 命中的原文)。"""
    m = _TIME_HM.search(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute, m.group(0)

    m = _TIME_CN.search(text)
    if m:
        period, hour_raw = m.group(1), int(m.group(2))
        tail = (m.group(3) or "").strip()
        minute = 30 if tail.startswith("半") else int(re.sub(r"\D", "", tail) or 0)
        if period in ("下午", "晚上", "晚", "中午", "今晚", "明晚", "夜里", "夜间") \
                and hour_raw < 12:
            hour_raw += 12
        if period == "凌晨" and hour_raw == 12:
            hour_raw = 0
        if 0 <= hour_raw <= 23 and 0 <= minute <= 59:
            return hour_raw, minute, m.group(0)
    return None


def resolve_location(text: str) -> tuple[str | None, str | None]:
    m = _LOC_TAG.search(text)
    if m:
        return m.group(1), m.group(0)
    for pattern in (_LOC_PLACE, _LOC_VERB):
        m = pattern.search(text)
        if not m:
            continue
        value = m.group(1).strip()
        if len(value) >= 2:
            # 「在6A305开会」里开会是动作不是地点：只摘「在6A305」，
            # 否则标题会被摘空，最后只能退回整句原文。
            matched = f"在{value}" if pattern is _LOC_VERB else m.group(0)
            return value, matched

    # 兜底：从场所关键词开始，向左最多扩展 4 个字符（"实验楼302" / "B203教室"）
    m = _LOC_BARE.search(text)
    if m:
        start, end, limit = m.start(), m.end(), _LOC_LEFT_LIMIT
        while start > 0 and limit > 0:
            char = text[start - 1]
            if not _LOC_CHAR.match(char) or char in _LOC_STOP_CHARS:
                break
            start -= 1
            limit -= 1
        value = text[start:end].strip()
        if len(value) >= 2 and not any(word in value for word in _BARE_LOC_BLACKLIST):
            return value, value
    return None, None


def is_noise(line: str) -> bool:
    """这行是不是"不该变成待办"的噪声（标题、寒暄、时间戳）。"""
    text = (line or "").strip()
    if len(text) < 2:
        return True
    if not re.search(r"[\w\u4e00-\u9fff]", text):
        return True
    for pattern in _NOISE_PATTERNS:
        if pattern.match(text):
            return True
    # 短句 + 标题类词 + 不含任何时间信息 → 判为标题/寒暄
    if len(text) <= _NOISE_MAX_LEN and any(word in text for word in _NOISE_WORDS):
        if resolve_date(text)[0] is None and resolve_time(text) is None:
            return True
    return False


def has_time_hint(text: str) -> bool:
    """这句话里有没有时间线索（今天/明天/周五/3点/11月20日…）。"""
    normalized = cn_time_to_digits(text)
    return resolve_date(normalized)[0] is not None or resolve_time(normalized) is not None


def text_without_time(text: str) -> str:
    """去掉时间片段后剩下的内容（只留汉字）。

    用来识别"只有时间、没有具体事情"的碎片，比如"周五之前"。
    占位符（[图片]/[回复]）本来就不是内容，一并去掉。
    """
    normalized = _PLACEHOLDER.sub(" ", cn_time_to_digits(to_simplified(text or "")))
    stripped = _DATE_STRIP.sub(" ", normalized)
    stripped = _TIME_STRIP.sub(" ", stripped)
    stripped = _STRIP_ALL.sub("", stripped)
    return re.sub(r"[^\u4e00-\u9fff]", "", stripped)


def _clean_title(text: str) -> str:
    """清掉摘除时间/地点后残留的碎片与标点。"""
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    # 群通知常见的称呼前缀：「各位同学：明天……」摘完时间剩「各位同学：集合」。
    # 分两条规则写是有意的 —— "同学"单独出现时可能是正题（"同学聚会"），
    # 只有后面跟着冒号才当称呼切掉。
    text = re.sub(r"^(?:各位|大家)\s*同学们?\s*[:：,，]?\s*", "", text)
    text = re.sub(r"^(?:同学们?)\s*[:：,，]\s*", "", text)
    text = re.sub(r"^(?:和|及|与|以及|然后|并且|另外|还有|就|要|需|请|记得)[、,，]?\s*", "", text)
    return text.strip(" ，,。、；;：:-—·*|\t")


def _build_title(raw: str) -> str:
    """从原文里摘出标题。摘完全是空的，就说明这行没有实质内容。

    这里是本项目修掉的一个**根因**（CONTRACT.md 第七节第 1 条）：
      旧项目在标题为空时会 `title = raw[:200]` 回退成原文，
      于是"10月9日-10月21日 12:20-13:50，17:50-18:50"这种纯时段表
      标题就是它自己 —— 用户看到的就是"只有时间"。
      改成：摘完什么都不剩 → 返回空字符串，由调用方判为碎片丢弃。
    """
    title = _DATE_STRIP.sub(" ", raw)
    title = _TIME_STRIP.sub(" ", title)
    _location, location_raw = resolve_location(raw)
    if location_raw:
        title = title.replace(location_raw, " ")
    # 时间/地点被摘掉后，括号里可能只剩空括号或日期碎片（"（ ）"、"（28号）"）
    title = re.sub(r"[（(【\[][\s、,，:：;；]*[）)】\]]", " ", title)
    title = re.sub(r"[（(【\[][^）)】\]]{0,8}(?:号|日|月|周|星期)"
                   r"[^）)】\]]{0,8}[）)】\]]", " ", title)
    title = _clean_title(title)
    return title[: int(config.get("title", "max_len", 200))]


def is_fragment(text: str) -> bool:
    """摘掉时间和编号后还有没有实打实的内容。

    这是"碎片"唯一的总判据（三档过滤强度最终都收敛到这一个函数）：
      · "拿快递" / "打游戏" → 有内容，不是碎片，必须保留；
      · "（A组）" / "5G" / "📅周一" / "10月9日-10月21日 12:20-13:50" → 是碎片。
    """
    body = text_without_time(text or "")
    minimum = int(config.get("title", "min_meaningful_chars", 2))
    return len(body) < minimum


def parse_line(line: str) -> TaskDraft | None:
    """把一行文本解析成一条待办草稿；噪声行、碎片行返回 None。"""
    raw = to_simplified(line or "")
    if raw.strip().startswith("#"):          # Markdown 标题不是待办
        return None
    raw = _BULLET.sub("", raw.strip())
    raw = _PLACEHOLDER.sub(" ", raw).strip()  # 去掉 [回复]/[图片] 这类占位符
    if is_noise(raw):
        return None
    if _FIELD_LINE.match(raw):
        return None                            # "截止时间：…" 这种字段行不是待办
    raw = cn_time_to_digits(raw)               # 中文数字时刻 → 阿拉伯数字

    if is_fragment(raw):
        return None                            # 摘完什么都不剩 → 碎片，丢掉

    date_part, _ = resolve_date(raw)
    time_part = resolve_time(raw)
    location, _location_raw = resolve_location(raw)

    due_at = _compose_due(date_part, time_part)
    title = _build_title(raw)
    if not title or is_fragment(title):
        return None
    if _FIELD_BARE.search(title):
        return None        # 光一个字段名（"集中办理时间"），不是一件事

    return TaskDraft(
        title=title,
        detail=" ".join(_URL.findall(raw)),
        due_at=due_at,
        location=location,
        remind_level=_remind_level(raw, due_at is not None),
        priority=_priority(raw, due_at is not None),
        source_kind=SOURCE_MANUAL,
    )


def _compose_due(date_part: datetime | None, time_part: tuple[int, int, str] | None) -> str | None:
    """把日期和时刻拼成截止时间字符串。"""
    if not date_part and not time_part:
        return None
    base = date_part or datetime.now()
    if time_part:
        hour, minute = time_part[0], time_part[1]
    else:
        # 只说了哪天、没说几点 → 当天 23:59。
        # 不写 09:00：一到九点这条就变"逾期"标红，可用户说的明明是"今天"。
        hour, minute = (int(x) for x in str(config.get("parse", "due_default_hour", "23:59")).split(":"))
    from ..contracts.models import TIME_FORMAT

    return base.replace(hour=hour, minute=minute, second=0, microsecond=0).strftime(TIME_FORMAT)


def _remind_level(text: str, has_due: bool) -> int:
    """提醒等级。旧项目的 L0~L4 简化成三档（CONTRACT.md 第二节"明确不做"）。"""
    if any(word in text for word in _URGENT + _MUST):
        return 3
    if "@我" in text or "全体" in text or "所有人" in text:
        return 2
    if has_due and any(word in text for word in _ACK + _IMPORTANT):
        return 2
    return 1


def _priority(text: str, has_due: bool) -> int:
    """调度优先级，数字越小越靠前。"""
    if any(word in text for word in _URGENT):
        return 0
    if "@我" in text:
        return 1
    if has_due:
        return 2
    return 3


def is_prose_block(lines: list[str]) -> bool:
    """这一堆行是"一整段话"还是"一份清单"？

    判据：行数够多，且大半的行摘掉时间后什么都不剩 —— 说明它们共同构成
    一段话，而不是各自一件事。招募通知、值班表都属于"一整段话"。
    """
    if len(lines) < PROSE_MIN_LINES:
        return False
    solid = 0
    for line in lines:
        if _looks_like_task(line, parse_line(line)):
            solid += 1
    return solid * 3 < len(lines)


def parse_text(text: str, strict: bool = False) -> list[TaskDraft]:
    """整段文本 → 待办草稿。

    短清单（用户一行一件事地敲/粘）照旧一行一条；
    长段落（一眼看去就是一大段话）整条合成一条，正文放进 detail。

    strict=True：别人的材料（群消息、群里的截图 OCR）要逐行再过一遍
    "这行自己像不像一件事"。收件箱（自己发给小号）不走这道 ——
    随手记"买牛奶"本来就没时间。
    """
    body = to_simplified(text or "").strip()
    if not body:
        return []
    lines = [line for line in body.splitlines() if line.strip()]
    drafted = [draft for line in lines if (draft := parse_line(line))]

    if is_prose_block(lines):
        # 大半的行根本不像一件事 ⇒ 这是一段话，不是清单
        return _as_one_draft(body, drafted)

    drafts: list[TaskDraft] = []
    seen: set[str] = set()
    for draft in drafted:
        key = f"{draft.title}|{draft.due_at or ''}"
        if key in seen:          # 同一段文字里的重复行只取一条
            continue
        seen.add(key)
        drafts.append(draft)
    return drafts


def _as_one_draft(body: str, drafted: list[TaskDraft]) -> list[TaskDraft]:
    """长段落 → 一条待办。标题取第一句像样的话，原文整段留在 detail。"""
    if not drafted:
        return []
    keep = drafted[0]
    keep.detail = body[:1000]      # 全文留着，消息就不"缺斤少两"
    return [keep]


def _looks_like_task(line: str, draft: TaskDraft | None) -> bool:
    """这行自己像一件事吗：得有明确钟点，去掉时间/地点后还得剩实打实的内容。

    招募通知里的 "8：50-12：00（A组）" 有钟点，但摘掉时间只剩"（A组）"，不算一件事。
    """
    if draft is None:
        return False
    raw = cn_time_to_digits(_PLACEHOLDER.sub(" ", (line or "").strip()))
    if resolve_time(raw) is None:
        return False
    return not is_fragment(draft.title)
