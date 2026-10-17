"""规则版文本解析（不使用 AI，零 Token、零联网）。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  改"什么时候算有日期"        → `resolve_date()` + 顶部 _DATE_* 正则
  改"什么时候算有时间"        → `resolve_time()` + 顶部 _TIME_* 正则
  改"哪些行算噪声"            → `is_noise()` + _NOISE_PATTERNS / _NOISE_WORDS
  改"什么样的碎片该丢掉"      → `is_fragment()` + config 的
                                title.min_meaningful_chars ← **最常调的就是这个**
  改标题怎么从原文里摘出来    → `_build_title()` + _DATE_STRIP / _TIME_STRIP
  改"哪些字段名不算待办"      → _FIELD_NOUNS
  改"多长的文字算一整段话"    → PROSE_MIN_LINES + `is_prose_block()`
  改地点怎么认                → `resolve_location()` + _PLACE_* 正则

**改完必须跑**：python -m unittest discover -s tests -t .
外加：python tools/baseline_scan.py --legacy <旧库导出> --db data/secretary.db
      （碎片率必须 <3%，而且"拿快递"这类不许被误伤）

**加正则时的注意**：中文里时间和内容经常粘在一起（"三点开会"），
所以每条正则后面都带有 `(?:以前|之前|前|后)?` 这类可选尾巴，
改的时候别把尾巴删了，否则"周五之前"会摘不干净。

=====================================================================
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

# ①-⑳（U+2460–U+2473）也当编号：学校通知里"①.②.③."太常见了。
_BULLET = re.compile(r"^\s*(?:[-*•·>#]|[\u2460-\u2473]\s*[.、)．,]?"
                     r"|\d+\s*[.、)．，,]|[（(]\d+[）)])\s*")
_URL = re.compile(r"https?://\S+")
# 聊天里的占位符不是内容，标题里要去掉。
# 第二段是 QQ 协议的原始代码（[CQ:at,qq=123] / [CQ:flashtransfer,...]）——
# 旧库 170 条里就有一条待办的标题是一整串 CQ 代码，手机上看就是天书。
_PLACEHOLDER = re.compile(
    # "全体成员"要排在"全体"前面：@全体成员 是完整称呼，漏掉"成员"会留下
    # 一个孤零零的"成员"头（2026-10-16 用户看到的 #44 标题就是"成员同学们好！…"）。
    r"\[CQ:[^\]]*\]|\[(?:回复|图片|表情|文件[^\]]*)\]|@(?:我|全体成员|全体|某人)"
)

# ---- 日期 ----
# ⚠️ 数字和「月/日/号」之间**允许有空格**（2026-10-13 用户真发过一条
# "10 月 14 号教学楼 612 下午 1 点半领交表"）：手机输入法、微信复制粘贴
# 很容易带空格。不带的话日期认不出来，会**退回今天** ——
# 那比不认更糟：提醒时间错了一整天，用户还以为程序算得准。
# 时刻那几条一直带着 `\s*`，日期这几条当初漏了。
_DATE_YMD = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DATE_MD = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
# 「10.11」「10.18」这种点分日期：学校通知里几乎都这么写
# （实测一条真通知："①.学生手册重考——10.11-10.23期间"、"（10.18日前）"）。
# 不认的话，那一行就成了"没有时间"的普通句子，整条通知的标题退化成
# 开头的「✨立即执行」——用户 2026-10-17 报的就是这个。
# 后面紧跟 元/块/折/分/%/小时 的不算日期（"6.5分"是分数、"3.5元"是钱）。
_DATE_DOT = re.compile(
    r"(?<![\d.])(\d{1,2})\s*[.．/]\s*(\d{1,2})"
    r"(?![\d.元块折分%小倍斤个人次万])")
# 「10.11-10.23」这种区间：截止是**后面**那天（区间是窗口，窗口到那天关）
_DATE_DOT_RANGE = re.compile(
    r"(?<![\d.])(\d{1,2})\s*[.．/]\s*(\d{1,2})"
    r"\s*[-—~～至到]\s*(\d{1,2})\s*[.．/]\s*(\d{1,2})"
    r"(?![\d.元块折分%小倍斤个人次万])")
_DATE_THIS_MONTH = re.compile(r"本月\s*(\d{1,2})\s*[日号]")
_DATE_NEXT_MONTH = re.compile(r"下月\s*(\d{1,2})\s*[日号]")
_DATE_REL = re.compile(r"(大后天|后天|今天|今晚|今早|明天|明晚|明早)")

# ---- 表格类整块：标题里写了这些字，就当"这是一张表" ----
# 见 should_merge 里的说明：这种表摘掉时间只剩一个人名，光看每行立不立得住分不出来。
_TABLE_WORD = re.compile(r"(值班|排班|班次|课表|时间表|日程|轮值)")
_DATE_WEEK = re.compile(r"(下周|本周|这周|周|星期)([一二三四五六日天1-7])")

# ---- 时刻 ----
_TIME_PERIOD = r"(上午|下午|中午|晚上|晚|早上|早晨|凌晨|今晚|明晚|夜里|夜间)?"
# ⚠️ 这条一定要带 `_TIME_PERIOD` 前缀。
#
# 真踩过：原来只写 `(\d{1,2}):(\d{2})`，于是"下午 3:00"被解析成 **03:00**
# （凌晨三点）—— 因为它压根没看前面那两个字。
# "下午3点开会"走的是另一条正则（带前缀），反而是对的。
# 同一种意思两种写法，一个对一个错，这种不一致最难发现。
_TIME_HM = re.compile(rf"{_TIME_PERIOD}\s*(\d{{1,2}})\s*[:：]\s*(\d{{2}})")
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
    r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    r"|(?<![\d.])\d{1,2}\s*[.．/]\s*\d{1,2}"
    r"(?![\d.元块折分%小倍斤个人次万])"
    r"|本月\s*\d{1,2}\s*[日号]|下月\s*\d{1,2}\s*[日号]"
    r"|大后天|后天|今天|明天|今晚|明晚|今早|明早"
    r"|(?:下周|本周|这周|周|星期)[一二三四五六日天1-7])(?:以前|之前|前|后|期间|之间)?"
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

# 合成一条时，原文在 detail 里最多留多少字
DETAIL_MAX_CHARS = 1000

# 机构名后缀。OCR 出来的海报/通知第一行经常是学校或单位抬头
# （"成都理工大学"），拿它当标题等于没标题。
ORG_SUFFIX = ("大学", "学院", "中学", "小学", "学校", "公司", "集团", "协会",
              "委员会", "中心", "医院", "研究院", "研究所", "工作室", "社团")


def my_names() -> list[str]:
    """用户在配置里填的自己的名字/昵称。

    用途：一张值班表里，属于你的那一行要单独拎出来 ——
    整张表合成一条之后，你仍然需要一眼看到"我周几值日"。
    """
    try:
        raw = config.get("parse", "my_names", []) or []
    except Exception:
        return []
    if isinstance(raw, str):
        raw = raw.replace("，", ",").split(",")
    return [str(item).strip() for item in raw if str(item).strip()]


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

    # 点分日期（"10.18"）。区间要在单个日期**之前**判：同样从第 0 个字符开始，
    # 谁先 consider 谁赢，而区间该取后一个日子。
    m = _DATE_DOT_RANGE.search(text)
    if m:
        try:
            consider(m.start(), today.replace(month=int(m.group(3)), day=int(m.group(4))),
                     m.group(0))
        except ValueError:
            pass
    m = _DATE_DOT.search(text)
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
        hour = _apply_period(m.group(1), int(m.group(2)))
        minute = int(m.group(3))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute, m.group(0)

    m = _TIME_CN.search(text)
    if m:
        period, hour_raw = m.group(1), int(m.group(2))
        tail = (m.group(3) or "").strip()
        minute = 30 if tail.startswith("半") else int(re.sub(r"\D", "", tail) or 0)
        hour_raw = _apply_period(period, hour_raw)
        if 0 <= hour_raw <= 23 and 0 <= minute <= 59:
            return hour_raw, minute, m.group(0)
    return None


# 「下午」类前缀 → 24 小时制。两种写法（"3:00" 和 "3点"）共用这一处逻辑，
# 免得改了一处忘了另一处 —— 上面那个 03:00 的 bug 就是这么来的。
_PM_PERIODS = ("下午", "晚上", "晚", "中午", "今晚", "明晚", "夜里", "夜间")


def _apply_period(period: str | None, hour: int) -> int:
    """把"下午3点""晚上7:30"换算成 24 小时制。"""
    if period in _PM_PERIODS and hour < 12:
        return hour + 12
    if period == "凌晨" and hour == 12:
        return 0
    return hour


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
        # 先转阿拉伯数字再判时间：不然"十点半"认不出来，这行就会被当寒暄扔掉
        # （2026-10-16：「接营长通知，军训时间暂时推迟到十点半」整条丢了）。
        normalized = cn_time_to_digits(text)
        if resolve_date(normalized)[0] is None and resolve_time(normalized) is None:
            return True
    return False


def has_time_hint(text: str) -> bool:
    """这句话里有没有时间线索（今天/明天/周五/3点/11月20日…）。"""
    normalized = cn_time_to_digits(text)
    return resolve_date(normalized)[0] is not None or resolve_time(normalized) is not None


def has_concrete_time(text: str) -> bool:
    """有没有**具体**的时间：几点、几月几号、周几。

    和 has_time_hint 的差别：只说了"今天/明天/后天"这类模糊天不算。
    为什么要分这么细（2026-10-13 用户待办里那 4 条误收）：群里天天有人说
    "明天要下雨""今天我拍了个照片" —— 光凭一个"明天"就收，待办列表会被
    聊天刷满，而且它们本来什么事都没有。
    """
    normalized = cn_time_to_digits(text)
    if resolve_time(normalized) is not None:
        return True
    return any(pattern.search(normalized) for pattern in
               (_DATE_YMD, _DATE_MD, _DATE_DOT, _DATE_THIS_MONTH,
                _DATE_NEXT_MONTH, _DATE_WEEK))


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
    # 通知里常见的「主题——具体说明」：破折号后面是细则，标题取前面那半截就够了
    # （细则本来就在正文里）。实测「②.理论学习和考试——（10.18日前）本周内请…」
    # 摘掉日期后剩「理论学习和考试—— 本周内请…」，带着一截尾巴。
    text = re.split(r"[—–]{2,}", text)[0]
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    # 摘掉地点后会留下"， 集合"这样的空档：中文标点不在 \u4e00-\u9fff 区间里，
    # 上面那条规则管不到，得单独清一次，否则标题会带着一个多余的空格。
    text = re.sub(r"([，,。、；;：:！!？?])\s+", r"\1", text)
    text = re.sub(r"\s+([，,。、；;：:！!？?])", r"\1", text)
    # 群通知常见的称呼前缀：「各位同学：明天……」摘完时间剩「各位同学：集合」。
    # 分两条规则写是有意的 —— "同学"单独出现时可能是正题（"同学聚会"），
    # 只有后面跟着冒号才当称呼切掉。
    text = re.sub(r"^(?:各位|大家)\s*同学们?\s*[:：,，]?\s*", "", text)
    text = re.sub(r"^(?:同学们?)\s*[:：,，]\s*", "", text)
    text = re.sub(r"^(?:和|及|与|以及|然后|并且|另外|还有|就|要|需|请|记得)[、,，]?\s*", "", text)
    return text.strip(" ，,。、；;：:-—·*|\t")


# 时间词前面只剩一个光杆介词：「推迟到10点」。这种时间**不摘** ——
# 摘掉会留下「推迟到，」这种半截话（2026-10-16 用户看到的标题就长这样）。
# 不摘的代价只是标题里留着时间，而截止时间照样算得出来，信息一个字不丢。
# 反过来做（把"到"删掉）就得维护一张"哪些词以'到'结尾"的词表：直到/签到/收到…
_DANGLING_PREPOSITIONS = "到于在为是从至"


def _strip_soft(text: str, pattern: re.Pattern) -> str:
    """摘掉一类时间片段，但"摘完会留下光杆介词"的那一处不摘。"""
    def _replace(match: "re.Match[str]") -> str:
        head = text[: match.start()].rstrip()
        if head and head[-1] in _DANGLING_PREPOSITIONS:
            return match.group(0)
        return " "

    return pattern.sub(_replace, text)


def _build_title(raw: str) -> str:
    """从原文里摘出标题。摘完全是空的，就说明这行没有实质内容。

    这里是本项目修掉的一个**根因**（CONTRACT.md 第七节第 1 条）：
      旧项目在标题为空时会 `title = raw[:200]` 回退成原文，
      于是"10月9日-10月21日 12:20-13:50，17:50-18:50"这种纯时段表
      标题就是它自己 —— 用户看到的就是"只有时间"。
      改成：摘完什么都不剩 → 返回空字符串，由调用方判为碎片丢弃。
    """
    title = _strip_soft(raw, _DATE_STRIP)
    title = _strip_soft(title, _TIME_STRIP)
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


def _priority(text: str, has_due: bool) -> int:
    """调度优先级，数字越小越靠前。"""
    if any(word in text for word in _URGENT):
        return 0
    if "@我" in text:
        return 1
    if has_due:
        return 2
    return 3


def solid_lines(lines: list[str]) -> list[tuple[str, TaskDraft]]:
    """哪些行**自己就立得住**：带时间线索，而且摘掉时间地点后还有实质内容。

    "下周一前提交实验报告" 立得住；"（A组）""8:50-12:00" 立不住。
    这是区分"一堆任务"和"一整块材料"的核心判据。

    ⚠️ 判据用的是「有没有时间线索」，**不是**「有没有钟点」。
    这里踩过一次：一开始要求必须有"几点几分"，结果"下周一前提交实验报告"
    因为只有日期没有钟点，被判成"立不住"，于是整份文件被合成一条 ——
    一份有两项作业的清单变成了一条。
    """
    found: list[tuple[str, TaskDraft]] = []
    for line in lines:
        draft = parse_line(line)
        if draft is None:
            continue
        if not has_time_hint(line):
            continue
        if is_fragment(draft.title):
            continue
        found.append((line, draft))
    return found


def is_schedule_block(lines: list[str]) -> bool:
    """整块是不是一张表（值班表 / 排班表 / 时间表）。

    判据：**每一行都带时间线索**（第一行若是标题——"下周值班表"这种——不算）。

    为什么这条能区分"表"和"带时间的清单"：
      · 清单（"明天交作业""后天开会"）—— 每行摘掉时间后还剩整件事，
        会被 solid_lines 挑出来，不算表；
      · 表（"周一 8:50-12:00 E1B516（A组）"）—— 每行都有时间，
        摘掉之后只剩"（A组）""6C205"这种坐标。

    实测：旧库里 5 行值班表被逐行判成碎片，**一条都没留下** ——
    用户看到的也是"我发的那张表没反应"。

    ⚠️ 2026-10-13 补：真的值班表**常常第一行是标题**（"下周值班表"），
    那一行没有时间 —— 原来要求"每一行都有时间"，于是整张表不成立，
    被拆成「张三」「李四」两条待办（离线模拟里真复现了）。
    现在允许把标题行摘掉再判；门槛也降到 2 行（真正拦住它的是
    `should_merge` 里那句"每行自己都立不住"，跟行数无关）。
    """
    rows = [line for line in lines if line.strip()]
    if rows and not has_time_hint(rows[0]):
        rows = rows[1:]                      # 第一行是标题，不算内容行
    if len(rows) < 2:
        return False
    return all(has_time_hint(line) for line in rows)


def should_merge(lines: list[str], from_file: bool) -> bool:
    """这一堆行要不要合成一条待办。"""
    solid = solid_lines(lines)
    if from_file:
        # 文件里读出来的内容**默认整份一条**：文件大部分是资料，不是清单。
        # 只有明确出现 ≥2 条能独立成立的待办，才认为它真是一份任务清单。
        # 实测：一份《实验报告要求.docx》4 行，按行切会切出
        # "字数不少于 2000 字""交到"这种垃圾。
        if len(solid) < 2:
            return True
        # 再加一条：**立得住的行不过半**时也整份合成一条。
        # 实测（2026-10-17）：一份《安全教育》通知 20 来行，只有 5 行"像待办"
        # （其余是"点击去学习""微课学习："这种），按行切就出 9 条碎片。
        # 真清单一一行一件事的话，占比不会低于一半。
        return len(solid) * 2 < len(lines)
    if is_schedule_block(lines) and _TABLE_WORD.search("\n".join(lines)):
        # 标题里明写了"值班表/排班表/课表"——**这就是一张表**，别管每行能不能
        # 独立成立：那种表长这样"张三 周一 8:00-12:00"，摘掉时间只剩一个人名，
        # 人名的"长度"和"拿快递"一样，`solid_lines` 分不出来（实测被拆成
        # 「张三」「李四」两条待办）。关键词是这里唯一可靠的信号。
        return True
    if _looks_like_document(lines):
        return True
    if len(lines) >= PROSE_MIN_LINES and len(solid) * 3 < len(lines):
        return True
    return is_schedule_block(lines) and len(solid) < 2


def is_prose_block(lines: list[str]) -> bool:
    """兼容旧叫法：行数够多、且大半的行自己立不住 → 是一整段话。"""
    return should_merge(lines, from_file=False)


def _looks_like_document(lines: list[str]) -> bool:
    """「标题行 + 编号子项」= **一份东西的正文**，不是一串并列待办。

    实测（2026-10-13，用户原话"它识别到的那个标题明显是错误的"）：
    这样一段
        实验报告要求
        1. 下周一前提交实验报告
        2. 字数不少于 2000 字
        3. 交到实验楼302
    按行切会切出「交到」「字数不少于 2000 字」两条标题 —— 谁看都不是待办。
    整段合成一条才对：标题「提交实验报告」，时间和地点都还在。

    但**真清单不能并**：用户一行一件事地敲时，第一行自己就带编号
    （"1. 交作业 / 2. 买牛奶"）—— 没有标题行，照样一行一条。
    """
    marked = [index for index, line in enumerate(lines) if _BULLET.match(line)]
    return len(marked) >= 2 and marked[0] > 0


def parse_text(text: str, strict: bool = False,
               from_file: bool = False) -> list[TaskDraft]:
    """整段文本 → 待办草稿。

    短清单（用户一行一件事地敲/粘）照旧一行一条；
    长段落、时间表、文件里的内容 → 整条合成一条，正文放进 detail。

    strict=True：别人的材料要**逐行**再过一遍"这行自己像不像一件事"。
      适用：群消息、群里的截图 OCR、**文件里读出来的字**。
      不适用：收件箱（自己发给小号）、自己敲的手动录入 ——
      随手记"买牛奶"本来就没时间。

    from_file=True：这段文字是附件里读出来的。它决定了"默认合不合成一条"。
    """
    body = to_simplified(text or "").strip()
    if not body:
        return []
    lines = [line for line in body.splitlines() if line.strip()]
    pairs = [(line, parse_line(line)) for line in lines]
    drafted = [draft for _line, draft in pairs if draft]

    if should_merge(lines, from_file):
        return _merge_block(body, lines, drafted)

    drafts: list[TaskDraft] = []
    seen: set[str] = set()
    # 延迟导入：actionable 要用本模块的时间识别，
    # 顶层 import 会变成循环依赖。
    from .actionable import is_actionable

    for line, draft in pairs:
        if draft is None:
            continue
        if strict:
            # 逐行再问一遍"这行自己像不像一件事"。
            # 真踩过的坑：一份 Word 里的"字数不少于 2000 字"、
            # 一张海报 OCR 出来的"成都理工大学"，都是这么漏进来的。
            ok, _why = is_actionable(line)
            if not ok:
                continue
        key = f"{draft.title}|{draft.due_at or ''}"
        if key in seen:          # 同一段文字里的重复行只取一条
            continue
        seen.add(key)
        drafts.append(draft)
    return drafts


def _merge_block(body: str, lines: list[str],
                 drafted: list[TaskDraft]) -> list[TaskDraft]:
    """把一整块内容合成**一条**待办，原文整段留在 detail。

    标题按优先级挑，不直接拿第一行 —— 实测讲座海报的第一行是
    "成都理工大学"，拿它当标题等于没标题。

      1. 自己能立得住的行（有时间 + 有实质内容）—— 信息最全
      2. 有实质内容、又不是机构抬头的行
      3. 实在没有，退回第一行原文（整块就是一张时间表时走这条）
    """
    # 时间表/排班表走单独一条路：标题直接说明"这是一张表"。
    #
    # 为什么不让某一行当标题：表里偶然有一行带着人名
    # （"周二 13:50-16:20 E1B412（张三）"），它在 is_fragment 眼里"有内容"
    # （剩下"张三"两个字），于是整张表的标题变成那一格。
    # 属于我的那几行由 `_my_lines()` 单独拎出来，不靠标题体现。
    table = is_schedule_block(lines)
    solid = solid_lines(lines)
    keep: TaskDraft | None = None
    if solid and not table:
        # 有时间优先；都有时间就取标题长一点的（"下周一前提交实验报告"
        # 比"提交"信息多）。max 取元组比较，长度先截到 20 免得
        # "志愿者委员会睡眠医学科专业组"那种长串赢过正经标题。
        keep = max(solid, key=lambda pair: (
            pair[1].due_at is not None, min(len(pair[1].title), 20)))[1]
    if keep is None and not table:
        keep = _first_meaningful_line(lines)
    if keep is None:
        raw = _clean_title(lines[0])[:50] or lines[0][:50]
        # 走到这里说明整块都是坐标和时段（值班表那种），第一行本身就是碎片。
        # 加个「表格：」前缀有双重作用：
        #   · 一眼能看出这是"一张表"，不是一件事；
        #   · 让它通过"标题不能是碎片"那道闸门 ——
        #     不加前缀的话标题就是 "周一 8:50-12:00 E1B516（A组）"，
        #     摘掉时间只剩一个"组"字，按定义就是碎片。
        keep = TaskDraft(title=f"表格：{raw}")

    # 挑中的标题那行可能没带时间，但整块里有。
    # 实测：一张讲座海报合成一条之后，标题是"学术讲座第82期"，
    # 可海报里明明写着"10月10日下午3:00" —— 不补的话这天该提醒你的事就没人提。
    #
    # 表格除外：一张值班表跨好几天，给它安一个"截止时间"是误导。
    if keep.due_at is None and not table:
        date_part, _ = resolve_date(body)
        time_part = resolve_time(body)
        keep.due_at = _compose_due(date_part, time_part)
    if keep.location is None and not table:
        location, _raw = resolve_location(body)
        keep.location = location

    keep.detail = body[:DETAIL_MAX_CHARS]
    results = [keep]

    # 表里属于自己的那几行单独拎出来。
    #
    # 为什么要有这一步：整张值班表合成一条之后，你打开看到的是一整块文字，
    # 还得自己找"我周几"。用户明确要过这个效果 ——
    # "可以让用户填自己的名称、昵称，可以拎出来强调"。
    results.extend(_my_lines(body, lines, keep))
    return results


def _my_lines(body: str, lines: list[str], merged: TaskDraft) -> list[TaskDraft]:
    """从一张表里挑出含"我"的名字的行，单独做一条待办。

    只有在整块是**表格**（多行同构、逐行都立不住）时才做这件事。
    一份正常的任务清单不需要 —— 里面每行本来就是独立的事。
    """
    names = my_names()
    if not names:
        return []
    if not is_schedule_block(lines):
        return []
    found: list[TaskDraft] = []
    for line in lines:
        if not any(name in line for name in names):
            continue
        draft = parse_line(line)
        if draft is None:
            # 这一行本身是碎片（"周一 8:50-12:00 B203"），
            # 但它带着我的名字，对我就是有效信息 —— 拿原文当标题。
            title = _clean_title(line)[:60] or line[:60]
            draft = TaskDraft(title=f"我的：{title}")
        else:
            draft.title = f"我的：{draft.title}"
        draft.detail = body[:DETAIL_MAX_CHARS]
        # 标一下来源，推送时能看出这条是从哪张表里挑出来的
        draft.quoted = draft.quoted or merged.quoted
        found.append(draft)
    return found


def _first_meaningful_line(lines: list[str]) -> TaskDraft | None:
    """挑第一行"像标题"的内容。

    两条经验：
      1. **跳过机构抬头** —— OCR 出来的海报第一行经常是"成都理工大学"，
         拿它当标题等于没标题；
      2. **不走 parse_line 的噪声过滤** —— 通知的第一行
         "关于招募志愿者的通知" 会被噪声规则当成寒暄过滤掉，
         可它恰恰是整份通知的标题。合并成整条时，首行就是标题。
    """
    for line in lines:
        if _FIELD_LINE.match(line.strip()):
            continue                      # "时间：…""地点：…" 是字段行
        candidate = _clean_title(
            _PLACEHOLDER.sub(" ", _BULLET.sub("", line.strip())))[:60]
        if len(candidate) < 2 or is_fragment(candidate):
            continue
        if any(candidate.endswith(suffix) for suffix in ORG_SUFFIX):
            continue
        draft = parse_line(line) or TaskDraft(title=candidate)
        draft.title = candidate
        return draft
    return None


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
