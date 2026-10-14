"""第 11 步门禁：课表插件（课表 → 待办）。

这个插件是**第一个真插件**，所以它要顺带证明两件事：

  1. 插件系统的「来源」槽是通的（注册进去 → --sync 能拉到东西）
  2. 三种输入格式都能吃：ICS / CSV 表格 / JSON

重点在解析上。坑都在细节里：ICS 的一行会被折成两行、转义反斜杠、
值里带冒号；表格的表头千奇百怪（"上课地点" vs "地点"）、
时间写法五花八门（08:00 / 8：00 / 8点 / 0800）。
这些错一个，用户看到的现象都是"课表导进来是空的 / 时间全错"。
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.pipeline.chain import Pipeline                           # noqa: E402
from app.pipeline.chat_filter import ChatNoiseEnricher            # noqa: E402
from app.pipeline.parse_enricher import RuleParseEnricher         # noqa: E402
from app.runtime import plugins                                   # noqa: E402
from app.runtime.config import Config as _Config                  # noqa: E402


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "plugin_timetable", ROOT / "plugins" / "timetable.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module      # dataclass(slots=True) 要求先登记
    spec.loader.exec_module(module)
    return module


timetable = load_plugin()

# 2026-10-12 是周一（下面有断言守着，哪天算错了这里会先炸）
MONDAY = dt.datetime(2026, 10, 12, 8, 0)

ONE_EVENT = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:course-1@school
DTSTART;TZID=Asia/Shanghai:20261012T080000
DTEND;TZID=Asia/Shanghai:20261012T094000
SUMMARY:高等数学
LOCATION:6A-305
DESCRIPTION:任课教师：张伟
END:VEVENT
END:VCALENDAR
"""

TABLE = """课程,星期,开始时间,结束时间,地点,周次
高等数学,周一,08:00,09:40,6A-305,1-16周
大学英语,周三,14:00,15:40,博学楼B203,双周
"""

JSON_COURSES = """[
  {"name": "线性代数", "weekday": "周四", "start": "10:00",
   "end": "11:40", "location": "A101", "weeks": "1-16周"},
  {"name": "体育", "date": "2026-10-13", "时间": "16:00-17:30", "地点": "东操场"}
]"""


def window(days: int = 7):
    """以周一为起点的窗口，方便算期望值。"""
    return MONDAY - dt.timedelta(hours=1), MONDAY + dt.timedelta(days=days)


def _write(name: str, text: str) -> Path:
    folder = Path(tempfile.mkdtemp(prefix="ps-timetable-"))
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


TABLE_FILE = _write("课表.csv", TABLE)


class TestIcsBasics(unittest.TestCase):
    def test_monday_fixture_is_really_monday(self):
        self.assertEqual(MONDAY.weekday(), 0)

    def test_unfold_joins_continuation_lines(self):
        """iCal 规定超长行要折起来，续行开头是一个空格。"""
        lines = timetable.unfold("SUMMARY:很长很长\n 的课程名\nLOCATION:A101")
        self.assertEqual(lines, ["SUMMARY:很长很长的课程名", "LOCATION:A101"])

    def test_unescape_handles_escapes(self):
        self.assertEqual(timetable.unescape(r"第一行\n第二行"), "第一行\n第二行")
        self.assertEqual(timetable.unescape(r"上课\,记得带书"), "上课,记得带书")
        self.assertEqual(timetable.unescape(r"反斜杠\\n不是换行"), r"反斜杠\n不是换行")

    def test_split_line_keeps_colon_inside_value(self):
        """DESCRIPTION 里常有网址，按第一个冒号切不能把值切坏。"""
        key, params, value = timetable.split_line(
            "DTSTART;TZID=Asia/Shanghai:20261012T080000")
        self.assertEqual(key, "DTSTART")
        self.assertEqual(params["TZID"], "Asia/Shanghai")
        self.assertEqual(value, "20261012T080000")

        _key, _params, url = timetable.split_line(
            "DESCRIPTION:见 https://example.com/a:b")
        self.assertEqual(url, "见 https://example.com/a:b")

    def test_parse_dt_three_shapes(self):
        self.assertEqual(timetable.parse_dt("20261012T080000"), MONDAY)
        self.assertEqual(timetable.parse_dt("20261012"), dt.datetime(2026, 10, 12))
        self.assertIsNone(timetable.parse_dt("这不是时间"))

    def test_parse_dt_utc_is_converted_to_local(self):
        """带 Z 的是 UTC，得按本机时区还原。"""
        utc = dt.datetime(2026, 10, 12, 0, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(timetable.parse_dt("20261012T000000Z"),
                         utc.astimezone().replace(tzinfo=None))


class TestParseEvents(unittest.TestCase):
    def test_one_event(self):
        events = timetable.parse_events(ONE_EVENT)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["summary"], "高等数学")
        self.assertEqual(event["location"], "6A-305")
        self.assertEqual(event["start"], MONDAY)
        self.assertEqual(event["end"], MONDAY + dt.timedelta(minutes=100))

    def test_garbage_does_not_crash(self):
        self.assertEqual(timetable.parse_events(""), [])
        self.assertEqual(timetable.parse_events("随便一段不是 iCal 的文字"), [])


class TestOccurrences(unittest.TestCase):
    def test_single_event_inside_window(self):
        event = timetable.parse_events(ONE_EVENT)[0]
        self.assertEqual(timetable.occurrences(event, *window()),
                         [(MONDAY, MONDAY + dt.timedelta(minutes=100))])

    def test_single_event_outside_window(self):
        event = timetable.parse_events(ONE_EVENT)[0]
        far = MONDAY + dt.timedelta(days=30)
        self.assertEqual(
            timetable.occurrences(event, far, far + dt.timedelta(days=7)), [])

    def test_weekly_rrule_count(self):
        """一学期 16 周 → 同一节课在 iCal 里常常是一条 RRULE，不是 16 条。"""
        event = {"start": MONDAY, "end": MONDAY + dt.timedelta(minutes=100),
                 "rrule": "FREQ=WEEKLY;COUNT=16"}
        got = timetable.occurrences(event, *window(days=7 * 20))
        self.assertEqual(len(got), 16)
        self.assertEqual(got[1][0], MONDAY + dt.timedelta(days=7))

    def test_weekly_rrule_byday(self):
        """BYDAY=MO,WE 表示一周上两次 → 两周内应该 4 次。"""
        event = {"start": MONDAY, "end": MONDAY + dt.timedelta(minutes=100),
                 "rrule": "FREQ=WEEKLY;BYDAY=MO,WE;COUNT=8"}
        got = timetable.occurrences(event, *window(days=14))
        self.assertEqual([m.weekday() for m, _ in got], [0, 2, 0, 2])

    def test_unsupported_frequency_is_skipped_not_crashed(self):
        """每天重复这种规则我们不展开，但必须留个标记让 fetch() 说出来。"""
        event = {"start": MONDAY, "rrule": "FREQ=DAILY"}
        self.assertEqual(timetable.occurrences(event, *window()), [])
        self.assertEqual(event["_unsupported"], "DAILY")

    def test_no_end_time_falls_back_to_default_lesson(self):
        got = timetable.occurrences({"start": MONDAY}, *window())
        self.assertEqual(got[0][1] - got[0][0],
                         dt.timedelta(minutes=timetable.DEFAULT_LESSON_MINUTES))


class TestFieldMatching(unittest.TestCase):
    def test_headers_are_matched_flexibly(self):
        """表头是用户/教务系统写的，说法千奇百怪，得都能认。"""
        for text, field in [("课程", "title"), ("课程名称", "title"),
                            ("科目", "title"), ("course", "title"),
                            ("上课地点", "location"), ("上课 地点", "location"),
                            ("教室", "location"), ("room", "location"),
                            ("开始时间", "start"), ("上课时间", "time"),
                            ("周次", "weeks"), ("星期", "weekday"),
                            ("日期", "date"), ("任课教师", "note")]:
            self.assertEqual(timetable.match_field(text), field, text)
        self.assertIsNone(timetable.match_field("第几周啊随便写的"))

    def test_parse_clock_shapes(self):
        for text, expected in [("08:00", dt.time(8, 0)), ("8：00", dt.time(8, 0)),
                               ("8点", dt.time(8, 0)), ("0800", dt.time(8, 0)),
                               ("14:05", dt.time(14, 5))]:
            self.assertEqual(timetable.parse_clock(text), expected, text)
        for bad in ("", "上午", "25:00", "9:99"):
            self.assertIsNone(timetable.parse_clock(bad), bad)

    def test_parse_date_shapes(self):
        expected = dt.date(2026, 10, 12)
        for text in ("2026-10-12", "2026/10/12", "2026年10月12日", "20261012"):
            self.assertEqual(timetable.parse_date(text), expected, text)
        self.assertEqual(timetable.parse_date("10月12日"),
                         dt.date(dt.date.today().year, 10, 12))
        self.assertIsNone(timetable.parse_date("下周"))

    def test_parse_weekday_shapes(self):
        for text, expected in [("周一", 0), ("星期一", 0), ("下周三", 2),
                               ("周7", 6), ("周日", 6), ("Mon", 0), ("FRIDAY", 4)]:
            self.assertEqual(timetable.parse_weekday(text), expected, text)
        self.assertIsNone(timetable.parse_weekday("每天"))
        self.assertIsNone(timetable.parse_weekday(""))

    def test_week_filter(self):
        keep = timetable.week_filter("1-4周")
        self.assertTrue(keep(2))
        self.assertFalse(keep(5))
        self.assertTrue(timetable.week_filter("1,3,5周")(3))
        self.assertFalse(timetable.week_filter("1,3,5周")(4))
        self.assertTrue(timetable.week_filter("单周")(3))
        self.assertFalse(timetable.week_filter("双周")(3))
        self.assertIsNone(timetable.week_filter(""))


class TestTableInput(unittest.TestCase):
    def test_csv_becomes_lessons(self):
        lessons = timetable.lessons_from_table(TABLE, *window())
        by_title = {lesson.title: lesson for lesson in lessons}
        self.assertEqual(set(by_title), {"高等数学", "大学英语"})
        self.assertEqual(by_title["高等数学"].start, MONDAY)
        self.assertEqual(by_title["高等数学"].location, "6A-305")
        # 周三 = 周一 + 2 天
        self.assertEqual(by_title["大学英语"].start,
                         MONDAY.replace(hour=14) + dt.timedelta(days=2))

    def test_tab_separated_also_works(self):
        text = TABLE.replace(",", "\t")
        self.assertEqual(len(timetable.lessons_from_table(text, *window())), 2)

    def test_chinese_comma_also_works(self):
        text = TABLE.replace(",", "，")
        self.assertEqual(len(timetable.lessons_from_table(text, *window())), 2)

    def test_weeks_column_ignored_without_term_start(self):
        """没填开学第一周时，"双周"没法算，只能当成每周都上 —— 不能因此整条丢掉。"""
        self.assertEqual(len(timetable.lessons_from_table(TABLE, *window())), 2)

    def test_weeks_column_filters_when_term_start_known(self):
        from app.runtime.config import Config

        original = timetable.config
        # 2026-10-12 就是第一周 → "双周"的大学英语这节课不该出现在这一周
        timetable.config = Config({"timetable": {"term_start": "2026-10-12"}})
        try:
            lessons = timetable.lessons_from_table(TABLE, *window())
        finally:
            timetable.config = original
        self.assertEqual([lesson.title for lesson in lessons], ["高等数学"])

    def test_headerless_table_gives_a_clear_error(self):
        with self.assertRaises(ValueError) as caught:
            timetable.lessons_from_table("高等数学,周一,08:00", *window())
        self.assertIn("表头", str(caught.exception))

    def test_row_without_time_is_dropped(self):
        text = "课程,星期,开始时间\n没有时间的课,周一,\n"
        self.assertEqual(timetable.lessons_from_table(text, *window()), [])


class TestJsonInput(unittest.TestCase):
    def test_json_array(self):
        lessons = timetable.lessons_from_json(JSON_COURSES, *window())
        by_title = {lesson.title: lesson for lesson in lessons}
        self.assertEqual(by_title["线性代数"].start, MONDAY.replace(hour=10)
                         + dt.timedelta(days=3))
        # 第二条用的是具体日期 + "16:00-17:30" 这种一格装两个时间
        self.assertEqual(by_title["体育"].start, dt.datetime(2026, 10, 13, 16, 0))
        self.assertEqual(by_title["体育"].location, "东操场")

    def test_json_wrapped_in_object(self):
        text = '{"lessons": [{"课程": "物理", "星期": "周五", "时间": "08:00"}]}'
        lessons = timetable.lessons_from_json(text, *window())
        self.assertEqual([lesson.title for lesson in lessons], ["物理"])

    def test_broken_json_raises(self):
        with self.assertRaises(ValueError):
            timetable.lessons_from_json("{不是 json", *window())


class TestSniff(unittest.TestCase):
    def test_detects_by_content(self):
        self.assertEqual(timetable.sniff(ONE_EVENT, "随便.txt"), "ics")
        self.assertEqual(timetable.sniff(JSON_COURSES, "随便.txt"), "json")
        self.assertEqual(timetable.sniff(TABLE, "课表.csv"), "table")

    def test_detects_by_extension_when_content_is_plain(self):
        self.assertEqual(timetable.sniff("课表内容", "a.ics"), "ics")
        self.assertEqual(timetable.sniff("课表内容", "a.json"), "json")

    def test_excel_is_refused_with_instructions(self):
        """Excel 不直接支持，但错误信息要说清楚怎么办 —— 不能只说"不支持"。"""
        with self.assertRaises(ValueError) as caught:
            timetable.sniff("PK随便写点", "课表.xlsx")
        self.assertIn("另存为", str(caught.exception))


class TestMessageAndPipeline(unittest.TestCase):
    def lesson(self):
        return timetable.Lesson(
            start=MONDAY, end=MONDAY + dt.timedelta(minutes=100),
            title="高等数学", location="6A-305", note="任课教师：张伟")

    def test_message_text_shape(self):
        message = timetable.message_for(self.lesson())
        self.assertTrue(message.msg_id.startswith("timetable:"))
        self.assertIn("10月12日 08:00 高等数学", message.text)
        self.assertIn("地点：6A-305", message.text)
        # 备注另起一行 —— 并到同一行会被解析器当成标题的一部分（实测）
        self.assertTrue(message.text.endswith("\n任课教师：张伟"))

    def test_msg_id_is_stable_across_syncs(self):
        """每次同步都换编号的话，消息表会一直堆新的。"""
        self.assertEqual(timetable.message_for(self.lesson()).msg_id,
                         timetable.message_for(self.lesson()).msg_id)

    def test_end_to_end_lands_as_a_clean_task(self):
        """本步真正的验收：一节课 → 标题/时间/地点全对。"""
        lessons, _skipped = timetable.lessons_from_ics(ONE_EVENT, *window())
        self.assertEqual(len(lessons), 1)

        message = timetable.message_for(lessons[0])
        pipe = Pipeline(draft_enrichers=[ChatNoiseEnricher(), RuleParseEnricher()])
        _prepared, drafts = pipe.run(message)

        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].title, "高等数学")
        self.assertEqual(drafts[0].due_at, "2026-10-12 08:00:00")
        self.assertEqual(drafts[0].location, "6A-305")


class TestPluginSlots(unittest.TestCase):
    def test_timetable_registers_into_the_source_slot(self):
        """真去加载 plugins/ 目录，确认来源槽通了。"""
        registry = plugins.Registry()
        statuses = plugins.load(ROOT, registry)

        by_name = {s.name: s for s in statuses}
        self.assertIn("timetable", by_name)
        self.assertTrue(by_name["timetable"].ok, by_name["timetable"].error)
        # 只断言"它在里面"。写死整个列表的话，以后每加一个来源插件这条就红一次，
        # 而那种红一个字的错都指不出来。
        self.assertIn("timetable", [s.kind for s in registry.sources])

    def test_source_without_target_produces_nothing(self):
        """没配订阅地址/文件时这个插件必须完全安静，不能报错、不能瞎拉。

        这里换掉插件手里的 config 对象，而不是靠本机 config.toml 里恰好没填 ——
        不然用户自己配好课表之后，这条测试就会跑去真的联网。
        """
        from app.runtime.config import Config

        original = timetable.config
        timetable.config = Config({"timetable": {"url": "", "file": ""}})
        try:
            self.assertEqual(timetable.TimetableSource().fetch(), [])
        finally:
            timetable.config = original


class TestTermStartNudge(unittest.TestCase):
    """课表里有"单双周/第几周"、但没填开学第一周时，必须**说一声**。

    不说的话，用户永远不知道有节课排错了 —— 只会觉得这软件算得不准。
    这是"什么时候让用户去配时间"的一个例子：不是一上来就问，
    而是**等这件事真的发生了**（他导了一份带周次的课表）再问。
    """

    def test_week_information_is_detected(self):
        self.assertTrue(timetable.has_week_info("高等数学,周一,08:00,,,1-16周"))
        self.assertTrue(timetable.has_week_info("单周上体育"))
        self.assertTrue(timetable.has_week_info("双周"))
        self.assertFalse(timetable.has_week_info("高等数学,周一,08:00,09:40,6A-305"))
        self.assertFalse(timetable.has_week_info(""))

    def test_fetch_notes_when_weeks_but_no_term_start(self):
        original = timetable.config
        timetable.config = _Config({"timetable": {"file": str(TABLE_FILE),
                                                  "term_start": ""}})
        try:
            source = timetable.TimetableSource()
            source.fetch()
        finally:
            timetable.config = original

        self.assertEqual(len(source.notes), 1)
        self.assertIn("开学第一周", source.notes[0])

    def test_no_note_once_term_start_is_filled(self):
        original = timetable.config
        timetable.config = _Config({"timetable": {"file": str(TABLE_FILE),
                                                  "term_start": "2026-09-01"}})
        try:
            source = timetable.TimetableSource()
            source.fetch()
        finally:
            timetable.config = original

        self.assertEqual(source.notes, [])

    def test_no_note_for_tables_without_week_info(self):
        path = _write("plain.csv",
                      "课程,星期,开始时间,结束时间,地点\n高等数学,周一,08:00,09:40,6A-305\n")
        original = timetable.config
        timetable.config = _Config({"timetable": {"file": str(path), "term_start": ""}})
        try:
            source = timetable.TimetableSource()
            source.fetch()
        finally:
            timetable.config = original

        self.assertEqual(source.notes, [])


if __name__ == "__main__":
    unittest.main()
