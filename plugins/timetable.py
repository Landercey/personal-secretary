"""课表插件：把课表变成待办。

=====================================================================
支持哪几种课表（按市面上实际有的挑的）
=====================================================================
查过一圈（GitHub 上几十个课表转换项目 + WakeUp/小爱/超级课程表这些 App
的导出功能），课表的通用出口其实就三类：

  1. **iCal / .ics** —— 最主流。WakeUp、小爱课程表、超级课程表都能导，
     很多学校的教务系统也直接给"订阅日历"的地址。支持**网址**和**本地文件**。
  2. **CSV / TSV 表格** —— 教务系统爱导 Excel，另存为 CSV 就是这个；
     也有工具直接导 CSV。一行一节课。
  3. **JSON** —— 转换脚本和 App 备份文件用的格式。一行一节课。

格式靠**内容嗅探**自动认，用户不用告诉程序"这是哪种"。

=====================================================================
故意不做的两种（做了只会更坑）
=====================================================================
· **"节次 × 星期"的网格课表**（屏幕截图、教务系统的格子表）：要把
  "第 3-4 节"换算成具体时刻，得知道**每所学校自己的作息表**；要知道
  "第 5 周"是哪天，得知道**开学第一周的周一**。这两样信息都不在图里，
  猜错的结果是一堆时间错误的待办 —— 比不做还坏。
· **登录教务系统抓课表**：账号密码 + 验证码 + 跟着学校改版走。
  拿用户的学号密码去冒险，换不来什么。

（截图这条路以后可以走 AI 插件：让模型看着图直接说"周几几点上什么"。
那是另一个层次的活，不塞在这个文件里。）

=====================================================================
怎么用
=====================================================================
    [timetable]
    url = "https://..."      # 订阅地址（webcal:// 也认）
    file = "D:/课表.csv"      # 或者本地文件（.ics / .csv / .json / .txt）
    days_ahead = 7           # 只把未来几天的课变成待办
    term_start = "2026-09-01"  # 想认"单双周/周次"才需要填

然后跑一次 `python run.py --sync`。
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app.contracts.interfaces import Source
from app.contracts.models import Message
from app.runtime.config import config

NAME = "timetable"
TITLE = "课表"
# 我的设置在设置面板的哪一段 —— 界面靠它给一个「去设置」的按钮。
# 插件自己声明，省得界面写死一张"哪个插件在哪配"的表（那张表一定会过期）。
CONFIG_SECTION = "timetable"
DESCRIPTION = "把课表（iCal 订阅 / CSV / JSON）变成待办"

# 服务器不爱理没带 UA 的请求，给一个常见的浏览器标识
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FETCH_TIMEOUT = 20                    # 秒
FETCH_MAX_BYTES = 5 * 1024 * 1024     # 课表撑死几百 KB，超过多半不是课表
READ_MAX_BYTES = 20 * 1024 * 1024     # 本地文件的上限
DEFAULT_LESSON_MINUTES = 45           # 只写了开始时间时，按一节 45 分钟算
MAX_WEEKS = 60                        # 循环展开上限（一学期约 20 周，留足余量）
NOTE_KEEP = 200                       # 备注最多留多少字

BYDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
WEEKDAY_EN = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

_UNESCAPE = re.compile(r"\\(.)")
_UNESCAPE_MAP = {"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}

# 表头/键名认识哪些说法。归一化（去掉空格和标点、转小写）之后再比。
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("课程", "课程名", "课程名称", "科目", "课名", "名称", "标题",
              "name", "title", "course", "subject"),
    "date": ("日期", "上课日期", "具体日期", "date"),
    "weekday": ("星期", "周几", "星期几", "礼拜", "weekday", "dow"),
    "weeks": ("周次", "周数", "上课周次", "单双周", "weeks", "week"),
    "time": ("时间", "上课时间", "时段", "time"),
    "start": ("开始时间", "开始", "起始时间", "start", "begin"),
    "end": ("结束时间", "结束", "下课时间", "end", "finish"),
    "location": ("地点", "教室", "上课地点", "上课教室", "位置",
                 "location", "room", "place"),
    "note": ("备注", "说明", "教师", "老师", "任课教师",
             "note", "remark", "teacher"),
}

# 归一化之后的别名 → 字段名。注意 "上课时间" 同时挂在 time 和 start 下，
# 谁先被定义谁赢：time 在前，所以单列"上课时间"会走"拆时间段"那条路。
_ALIAS_INDEX: dict[str, str] = {}
for _field, _aliases in FIELD_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_INDEX.setdefault(re.sub(r"[\s_\-:：()（）\[\]]+", "", _alias).lower(),
                                _field)


@dataclass(slots=True)
class Lesson:
    """一节课。三种输入格式最后都变成它，再变成 Message。"""

    start: dt.datetime
    end: dt.datetime
    title: str
    location: str = ""
    note: str = ""


# --------------------------------------------------------------------- 通用小工具
def normalize_key(text: object) -> str:
    """表头/键名归一化：去空格标点、转小写。`"上课 地点"` 和 `"上课地点"` 要能对上。"""
    return re.sub(r"[\s_\-:：()（）\[\]]+", "", str(text or "")).lower()


def match_field(key: object) -> str | None:
    """一个表头/键名对应到哪个字段。认不出来返回 None。"""
    return _ALIAS_INDEX.get(normalize_key(key))


def parse_clock(text: object) -> dt.time | None:
    """从 `08:00` / `8：00` / `8点` / `0800` 里抠出时刻。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    found = re.search(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", raw)
    if found:
        hour, minute = int(found.group(1)), int(found.group(2))
    else:
        found = re.search(r"(\d{1,2})\s*[点時时]", raw)
        if found:
            hour, minute = int(found.group(1)), 0
        else:
            found = re.fullmatch(r"(\d{3,4})", raw)
            if not found:
                return None
            digits = found.group(1)
            hour, minute = int(digits[:-2]), int(digits[-2:])
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return dt.time(hour, minute)


def split_time_range(text: object) -> tuple[object, object]:
    """`08:00-09:40` 拆成两半。拆不开就把整串当开始时间。"""
    pieces = re.split(r"\s*[-–—~～至到]\s*", str(text or ""), maxsplit=1)
    return (pieces[0], pieces[1]) if len(pieces) == 2 else (pieces[0], "")


def parse_date(text: object) -> dt.date | None:
    """认 `2026-10-12` / `2026/10/12` / `2026年10月12日` / `20261012` / `10月12日`。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    found = re.search(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", raw)
    if found:
        return _safe_date(int(found.group(1)), int(found.group(2)), int(found.group(3)))
    found = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", raw)
    if found:
        return _safe_date(dt.date.today().year, int(found.group(1)), int(found.group(2)))
    found = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", raw)
    if found:
        return _safe_date(int(found.group(1)), int(found.group(2)), int(found.group(3)))
    return None


def _safe_date(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def parse_weekday(text: object) -> int | None:
    """`周一` / `星期一` / `礼拜一` / `周1` / `Mon` → 0..6（周一是 0）。"""
    raw = normalize_key(text)
    # 「每天」不是星期几。少了这一条，它的末字"天"会被当成周日（真踩过）。
    if not raw or "每" in raw:
        return None
    for word, index in WEEKDAY_EN.items():
        if raw.startswith(word):
            return index
    found = re.search(r"(?:周|星期|礼拜|礼)([一二三四五六日天1-7])", raw)
    if found:
        token = found.group(1)
    elif len(raw) == 1:
        token = raw
    else:
        return None
    if token.isdigit():
        number = int(token)
        return number - 1 if 1 <= number <= 7 else None
    return WEEKDAY_CN.get(token)


_WEEK_LIST = re.compile(
    r"(\d{1,2}(?:\s*[-–—~～]\s*\d{1,2})?"
    r"(?:\s*[,，、]\s*\d{1,2}(?:\s*[-–—~～]\s*\d{1,2})?)*)"
    r"\s*周")


def week_filter(text: object):
    """`周次` 列 → 一个判断函数：第 N 周上不上这节课。

    认三种写法：`1-16周` / `1,3,5周` / `单周` `双周`。
    什么都没写就返回 None（不筛）。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    found = _WEEK_LIST.search(raw)
    if found:
        wanted: set[int] = set()
        for piece in re.split(r"[,，、]", found.group(1)):
            piece = re.sub(r"\s*[-–—~～]\s*", "-", piece.strip())
            if "-" in piece:
                left, _, right = piece.partition("-")
                if left.isdigit() and right.isdigit():
                    wanted.update(range(int(left), int(right) + 1))
            elif piece.isdigit():
                wanted.add(int(piece))
        if wanted:
            return lambda week, wanted=wanted: week in wanted
    if "单周" in raw:
        return lambda week: week % 2 == 1
    if "双周" in raw:
        return lambda week: week % 2 == 0
    return None


# --------------------------------------------------------------------- ICS 解析
def unfold(text: str) -> list[str]:
    """把折行的 ICS 还原成"一条一行"。

    iCal 规定一行超过 75 字节就在下一行开头放一个空格续上。
    不还原的话，长课程名/长备注会被从中间劈开。
    """
    lines: list[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def unescape(value: str) -> str:
    """还原 iCal 的转义：`\\n` 换行、`\\,` 逗号、`\\;` 分号、`\\\\` 反斜杠。

    一次扫描替换，不能连着 .replace() —— `\\\\n`（转义的反斜杠 + 字母 n）
    会被错认成换行。
    """
    return _UNESCAPE.sub(lambda m: _UNESCAPE_MAP.get(m.group(1), m.group(1)), value)


def split_line(line: str) -> tuple[str, dict[str, str], str]:
    """`DTSTART;TZID=Asia/Shanghai:20261013T080000` → 名字、参数、值。

    用 partition 而不是 split(":") —— DESCRIPTION 的值里常带冒号（网址），
    按第一个冒号切才不会切坏。
    """
    head, _, value = line.partition(":")
    pieces = head.split(";")
    params: dict[str, str] = {}
    for piece in pieces[1:]:
        key, _, raw = piece.partition("=")
        params[key.strip().upper()] = raw.strip().strip('"')
    return pieces[0].strip().upper(), params, value


def parse_dt(value: str) -> dt.datetime | None:
    """iCal 的时间戳 → 朴素本地时间。

    三种写法都认：`20261013T080000Z`（UTC）、`20261013T080000`、`20261013`。

    **TZID 参数忽略掉，按墙上时间处理。** 理由：国内课表订阅几乎全是
    Asia/Shanghai，而 Windows 上的 Python 默认不带 tzdata，
    真去按 IANA 时区名解会直接抛异常 —— 为了一个基本用不上的场景，
    把插件的依赖从"零"变成"要装 tzdata"，不划算。
    """
    value = value.strip()
    try:
        if value.endswith("Z"):
            utc = dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
            return utc.replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None)
        if "T" in value:
            return dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
        return dt.datetime.strptime(value[:8], "%Y%m%d")
    except ValueError:
        return None


def parse_events(text: str) -> list[dict]:
    """把 VCALENDAR 拆成一堆 VEVENT 字典。只认我们真正用得到的字段。"""
    events: list[dict] = []
    current: dict | None = None
    for line in unfold(text):
        key, _params, value = split_line(line)
        if key == "BEGIN" and value.strip().upper() == "VEVENT":
            current = {}
        elif key == "END" and value.strip().upper() == "VEVENT":
            if current is not None:
                events.append(current)
            current = None
        elif current is not None:
            if key == "UID":
                current["uid"] = value.strip()
            elif key == "SUMMARY":
                current["summary"] = unescape(value).strip()
            elif key == "LOCATION":
                current["location"] = unescape(value).strip()
            elif key == "DESCRIPTION":
                current["description"] = unescape(value).strip()
            elif key == "DTSTART":
                current["start"] = parse_dt(value)
            elif key == "DTEND":
                current["end"] = parse_dt(value)
            elif key == "RRULE":
                current["rrule"] = value.strip()
    return events


def parse_rrule(value: str) -> dict[str, str]:
    """`FREQ=WEEKLY;INTERVAL=1;COUNT=16` → 字典。"""
    rule: dict[str, str] = {}
    for piece in (value or "").split(";"):
        key, _, raw = piece.partition("=")
        if key:
            rule[key.strip().upper()] = raw.strip()
    return rule


def occurrences(event: dict, window_start: dt.datetime,
                window_end: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
    """这节课在时间窗里有哪几次。返回 [(开始, 结束)]。

    只展开 `FREQ=WEEKLY` —— 课表里循环事件 99% 是"每周同一时间上到学期末"。
    别的频率（每天、每月）原样跳过，由调用方统计出来告诉用户。
    """
    start = event.get("start")
    if not isinstance(start, dt.datetime):
        return []
    end = event.get("end") if isinstance(event.get("end"), dt.datetime) else None
    span = (end - start) if end else dt.timedelta(minutes=DEFAULT_LESSON_MINUTES)

    rule = parse_rrule(event.get("rrule", ""))
    if not rule:
        return [(start, start + span)] if window_start <= start < window_end else []
    if rule.get("FREQ", "").upper() != "WEEKLY":
        event["_unsupported"] = rule.get("FREQ", "?")
        return []

    try:
        interval = max(1, int(rule.get("INTERVAL") or 1))
    except ValueError:
        interval = 1
    until = parse_dt(rule.get("UNTIL", "")) if rule.get("UNTIL") else None
    try:
        count = int(rule.get("COUNT") or 0)
    except ValueError:
        count = 0
    days = [BYDAY[d] for d in re.split(r"[,\s]+", rule.get("BYDAY", "")) if d in BYDAY]

    moments: list[dt.datetime] = []
    cursor = start
    for _ in range(MAX_WEEKS):
        if until and cursor > until:
            break
        if cursor >= window_end:
            break
        if days:
            week = cursor - dt.timedelta(days=cursor.weekday())
            for slot in sorted(set(days)):
                moment = (week + dt.timedelta(days=slot)).replace(
                    hour=start.hour, minute=start.minute, second=start.second)
                if moment < start or (until and moment > until):
                    continue
                if window_start <= moment < window_end:
                    moments.append(moment)
        elif window_start <= cursor < window_end:
            moments.append(cursor)
        cursor += dt.timedelta(days=7 * interval)

    moments.sort()
    if count:
        moments = moments[:count]
    return [(moment, moment + span) for moment in moments]


def lessons_from_ics(text: str, window_start: dt.datetime,
                     window_end: dt.datetime) -> tuple[list[Lesson], int]:
    """ICS → 课程。第二个返回值是"没能展开的循环条数"。"""
    lessons: list[Lesson] = []
    skipped = 0
    for event in parse_events(text):
        title = event.get("summary") or "课程"
        note = (event.get("description") or "").strip()
        location = (event.get("location") or "").strip()
        for start, end in occurrences(event, window_start, window_end):
            lessons.append(Lesson(start=start, end=end, title=title,
                                  location=location, note=note))
        if event.get("_unsupported"):
            skipped += 1
    return lessons, skipped


# --------------------------------------------------------------------- 表格 / JSON
def detect_delimiter(text: str) -> str:
    """猜分隔符：看第一行里哪一种出现得最多。"""
    lines = text.splitlines()
    head = lines[0] if lines else ""
    counts = {symbol: head.count(symbol) for symbol in (",", "\t", ";", "，")}
    best = max(counts, key=counts.get)
    if counts[best] == 0:
        raise ValueError("认不出表格的分隔符（第一行里没有逗号也没有制表符）")
    return best


def find_header(rows: list[list[str]]) -> tuple[int, dict[str, int]] | None:
    """找出表头在哪一行、哪一列是哪个字段。

    要求至少认出「课程」和「开始时间（或时间）」——
    缺这两个就没法变成待办，早点报错比猜出一堆错的强。
    """
    for index, row in enumerate(rows[:20]):
        mapping: dict[str, int] = {}
        for column, cell in enumerate(row):
            field = match_field(cell)
            if field and field not in mapping:
                mapping[field] = column
        if "title" in mapping and ("start" in mapping or "time" in mapping):
            return index, mapping
    return None


def lessons_from_table(text: str, window_start: dt.datetime,
                       window_end: dt.datetime) -> list[Lesson]:
    """CSV / TSV / 纯文本表格 → 课程。一行一节课。"""
    rows = list(csv.reader(io.StringIO(text), delimiter=detect_delimiter(text)))
    header = find_header(rows)
    if header is None:
        raise ValueError(
            "认不出表头。第一行要是列名，至少得有「课程」和「开始时间」"
            "（例如：课程,星期,开始时间,结束时间,地点）")
    header_index, mapping = header

    records: list[dict[str, str]] = []
    for row in rows[header_index + 1:]:
        record = {field: (row[column] if column < len(row) else "").strip()
                  for field, column in mapping.items()}
        if record.get("title"):
            records.append(record)
    return rows_to_lessons(records, window_start, window_end)


def lessons_from_json(text: str, window_start: dt.datetime,
                      window_end: dt.datetime) -> list[Lesson]:
    """JSON → 课程。最外层是数组，或者 `{"lessons": [...]}`。"""
    data = json.loads(text)
    if isinstance(data, dict):
        for key in ("lessons", "courses", "classes", "items", "课表", "课程"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        raise ValueError("JSON 最外层要是数组，或者有 lessons / courses 字段")

    records: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        record: dict[str, str] = {}
        for key, value in item.items():
            field = match_field(key)
            if field and value not in (None, "") and field not in record:
                record[field] = str(value)
        if record.get("title"):
            records.append(record)
    return rows_to_lessons(records, window_start, window_end)


def rows_to_lessons(rows: list[dict[str, str]], window_start: dt.datetime,
                    window_end: dt.datetime) -> list[Lesson]:
    """一批「一节课」记录 → 落进时间窗的 Lesson。表格和 JSON 共用这一份。"""
    term_start = parse_date(config.get("timetable", "term_start", ""))
    lessons: list[Lesson] = []

    for row in rows:
        title = (row.get("title") or "").strip()
        if not title:
            continue
        location = (row.get("location") or "").strip()
        note = (row.get("note") or "").strip()

        # 时间：可能是 "08:00" + "09:40" 两列，也可能是 "08:00-09:40" 一列
        start_text = row.get("start", "")
        end_text = row.get("end", "")
        if not start_text and row.get("time"):
            start_text, end_text = split_time_range(row["time"])
        clock = parse_clock(start_text)
        if clock is None:
            continue                                    # 没时间的当不了待办
        end_clock = parse_clock(end_text)

        keep = week_filter(row.get("weeks"))
        for day in target_days(row, window_start, window_end):
            # 周次只有知道开学第一周是哪天才有意义，没配就当成每周都上
            if keep and term_start:
                if not keep((day - term_start).days // 7 + 1):
                    continue
            start = dt.datetime.combine(day, clock)
            if not (window_start <= start < window_end):
                continue
            end = (dt.datetime.combine(day, end_clock) if end_clock
                   else start + dt.timedelta(minutes=DEFAULT_LESSON_MINUTES))
            lessons.append(Lesson(start=start, end=end, title=title,
                                  location=location, note=note))
    return lessons


def target_days(row: dict[str, str], window_start: dt.datetime,
                window_end: dt.datetime) -> list[dt.date]:
    """这一行说的是哪几天。有具体日期就用它，否则按"星期几"每周循环。"""
    explicit = parse_date(row.get("date"))
    if explicit:
        return [explicit]
    weekday = parse_weekday(row.get("weekday"))
    if weekday is None:
        return []            # 既没日期也没星期：不知道是哪天，不猜
    days: list[dt.date] = []
    day = window_start.date()
    last = window_end.date()
    while day <= last:
        if day.weekday() == weekday:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


# --------------------------------------------------------------------- 读文件
def read_target(target: str) -> str:
    """网址或本地文件都能读。`webcal://` 是 iCal 界的方言，等于 https。"""
    if target.startswith("webcal://"):
        target = "https://" + target[len("webcal://"):]
    if target.lower().startswith(("http://", "https://")):
        request = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            blob = response.read(FETCH_MAX_BYTES)
    else:
        blob = Path(target).read_bytes()
        if len(blob) > READ_MAX_BYTES:
            raise ValueError("文件太大了，看着不像课表")
    # 有些导出工具会带上 UTF-8 BOM，不剥掉的话第一行会变成
    # "\ufeffBEGIN:VCALENDAR"，整个文件解析出来是空的。
    return blob.decode("utf-8-sig", errors="replace")


def sniff(text: str, name: str = "") -> str:
    """这是哪种格式？先看内容，认不出再退回来看扩展名。"""
    head = text.lstrip()[:200].upper()
    if head.startswith("BEGIN:VCALENDAR"):
        return "ics"
    if head[:1] in ("{", "["):
        return "json"
    path = name.split("?")[0].split("#")[0]
    suffix = Path(path).suffix.lower()
    if suffix in (".ics", ".ical"):
        return "ics"
    if suffix == ".json":
        return "json"
    if suffix in (".xlsx", ".xls"):
        raise ValueError(
            "Excel 直接读不了。在 Excel 里「另存为 → CSV UTF-8」，"
            "再把 [timetable] file 指到那个 .csv")
    return "table"


def lessons_from_text(text: str, name: str, window_start: dt.datetime,
                      window_end: dt.datetime) -> tuple[list[Lesson], int]:
    kind = sniff(text, name)
    if kind == "ics":
        return lessons_from_ics(text, window_start, window_end)
    if kind == "json":
        return lessons_from_json(text, window_start, window_end), 0
    return lessons_from_table(text, window_start, window_end), 0


# --------------------------------------------------------------------- 变成消息
def lesson_key(lesson: Lesson) -> str:
    """这条消息的稳定编号。

    必须**跨次同步稳定**：每次 --sync 都生成新编号的话，
    消息表里会一直堆新的，同一节课的待办也会被反复"当成新的"。
    """
    digest = hashlib.sha1(
        f"{lesson.title}|{lesson.start.isoformat()}|{lesson.location}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{digest}@{lesson.start.strftime('%Y%m%dT%H%M%S')}"


def message_for(lesson: Lesson) -> Message:
    """一节课 → 一条消息。

    正文格式是**照着规则解析器的口味写的**（实测挑出来的）：
      `10月13日 08:00 高等数学 地点：6A-305`
    这样标题只剩课程名、时间进 due_at、教室进 location。
    把「教师：张三」并到同一行会被当成标题的一部分，所以备注另起一行。
    """
    text = (f"{lesson.start.strftime('%m月%d日')} "
            f"{lesson.start.strftime('%H:%M')} {lesson.title}")
    if lesson.location:
        text += f" 地点：{lesson.location}"
    if lesson.note:
        text += "\n" + lesson.note[:NOTE_KEEP]

    return Message(
        msg_id="timetable:" + lesson_key(lesson),
        source_kind="timetable",
        source_name="课表",
        text=text,
        raw={"title": lesson.title, "location": lesson.location},
    )


# --------------------------------------------------------------------- 来源
class TimetableSource(Source):
    """只拉不推的来源。什么时候拉由 `run.py --sync` 决定。"""

    kind = "timetable"
    title = TITLE

    def __init__(self) -> None:
        # 上次拉取时"有话要跟用户说"的内容，界面拿它显示。
        # 存在实例上是因为装配时只 new 一次，之后一直复用同一个实例。
        self.notes: list[str] = []

    def submit(self, message: Message) -> Message:
        return message        # 课表只有"拉"，没有"推"

    def fetch(self) -> list[Message]:
        self.notes = []
        targets = [
            str(config.get("timetable", key, "") or "").strip()
            for key in ("url", "file")
        ]
        targets = [target for target in targets if target]
        if not targets:
            return []
        try:
            days = int(config.get("timetable", "days_ahead", 7) or 7)
        except (TypeError, ValueError):
            days = 7
        now = dt.datetime.now()
        horizon = now + dt.timedelta(days=max(1, days))

        out: list[Message] = []
        for target in targets:
            text = read_target(target)
            lessons, skipped = lessons_from_text(text, target, now, horizon)
            if skipped:
                # 不静默：用户看到"课表怎么少了几门"时得能自己查出来
                print(f"  ⚠️ 有 {skipped} 条循环规则不是「每周重复」，"
                      f"没有展开（只支持 FREQ=WEEKLY）")
            if not str(config.get("timetable", "term_start", "") or "").strip() \
                    and has_week_info(text):
                # 这条也要说出去：不说的话，用户永远不知道有节课时间排错了
                self.notes.append(
                    "课表里写了「单周 / 双周 / 周次」，但没填开学第一周的周一 —— "
                    "现在按每周都上处理。到「设置 → 收消息 → 课表」补一下会更准。")
            out.extend(message_for(lesson) for lesson in lessons)
        return out


def has_week_info(text: str) -> bool:
    """这份课表里有没有"第几周 / 单双周"这类信息。

    有的话，不知道开学第一周是哪天就只能当成"每周都上"，
    单双周排课会排错 —— 所以要提示用户去补 `term_start`。
    """
    return bool(_WEEK_LIST.search(text or "")) or "单周" in text or "双周" in text


def register(registry) -> None:
    registry.add_source(TimetableSource())
