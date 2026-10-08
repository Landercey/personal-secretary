"""第 6 步门禁：识别质量。全部样本来自真实数据。

硬指标（CONTRACT.md 第八节）：
  · 碎片率 < 3%  —— 这里直接断言**产出的标题永远不是碎片**（等价于 0%）
  · 短待办零误伤 —— "拿快递""打游戏"一条都不许丢
  · 排班表不再碎 —— 整段合成一条，不是 0 条也不是 5 条

这些样本不是编的：海报和手写作业照来自真实 QQ 群，值班表来自旧库里的
真实条目，Word 内容按真实作业要求写。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.pipeline import quick_parse as q                       # noqa: E402

# 一张讲座海报 OCR 出来的原文（真实，32 行缩到 14 行）
POSTER = """成都理工大学
学术讲座第82期
某个讲座主题

周晓波
主讲嘉宾：
四川省人民医院
医学博士、主治医师
培训时间
培训地点
10月10日下午3:00
6C205
会议号：111222333
【名额有限先到先得】"""

# 一份作业要求（真实结构）
HOMEWORK_ONE = """实验报告要求
1. 下周一前提交实验报告
2. 字数不少于 2000 字
3. 交到实验楼302"""

HOMEWORK_TWO = """实验报告要求
1. 下周一前提交实验报告
2. 周五前交数学作业"""

# 旧库里那批值班表碎片（真实条目）
ROSTER = """周一 8:50-12:00 E1B516（A组）
周一 9:50-12:00 E1A504（B组）
周二 13:50-16:20 E1B412（C组）
周二 13:50-16:20 C075-05-32（D组）
周三 14:00-16:00 E1B516（E组）"""

# 群里转发的招募通知（真实结构）
NOTICE = """关于招募志愿者的通知
各位同学：
本周末开展志愿活动，具体安排如下：
时间：10月11日 8:50-12:00
地点：E1B516
分组：A组 B组 C组
请有意向的同学在群里回复
谢谢大家"""


def parse(text: str, *, from_file: bool) -> list:
    return q.parse_text(text, strict=from_file, from_file=from_file)


class TestFragmentRate(unittest.TestCase):
    """硬指标：产出的标题里不许有碎片。"""

    ALL_INPUTS = [
        (POSTER, True),
        (HOMEWORK_ONE, True),
        (HOMEWORK_TWO, True),
        (ROSTER, True),
        (NOTICE, False),
        ("买牛奶\n拿快递\n写作业", False),
        ("明天下午三点在图书馆门口集合", False),
    ]

    def test_no_produced_title_is_a_fragment(self):
        """碎片率 0% —— 比契约要求的 <3% 更严。

        为什么敢断言 0：碎片在**入口**就被挡掉了（is_fragment 是硬闸门），
        所以只要闸门还在，产出里就不可能有碎片。这条测试保的就是那道闸门。
        """
        for text, from_file in self.ALL_INPUTS:
            for draft in parse(text, from_file=from_file):
                with self.subTest(title=draft.title):
                    self.assertFalse(
                        q.is_fragment(draft.title),
                        f"产出里出现了碎片标题：{draft.title!r}")

    def test_no_title_is_bare_field_name(self):
        """产出里也不许有"时间""地点""分组"这种纯栏名。"""
        for text, from_file in self.ALL_INPUTS:
            for draft in parse(text, from_file=from_file):
                with self.subTest(title=draft.title):
                    self.assertFalse(q._FIELD_BARE.search(draft.title))


class TestShortTodosNotHurt(unittest.TestCase):
    """短待办零误伤。"""

    def test_self_notes_survive(self):
        for text in ("拿快递", "取快递", "打游戏", "买牛奶", "交作业"):
            with self.subTest(text=text):
                drafts = parse(text, from_file=False)
                self.assertEqual(len(drafts), 1, f"短待办被误伤了：{text}")
                self.assertEqual(drafts[0].title, text)

    def test_short_list_still_splits(self):
        """自己敲的短清单不能被"整份合成一条"误伤。"""
        drafts = parse("买牛奶\n拿快递\n写作业", from_file=False)
        self.assertEqual(len(drafts), 3)


class TestRoster(unittest.TestCase):
    """值班表：整段合成一条。"""

    def test_roster_becomes_one_task_not_zero(self):
        """回归：旧逻辑把 5 行值班表逐行判成碎片，**一条都没留下**。

        用户看到的现象是"我发的那张表没反应"，而且没有任何提示。
        """
        drafts = parse(ROSTER, from_file=True)
        self.assertEqual(len(drafts), 1, "值班表应该合成一条")
        self.assertIn("E1B516", drafts[0].title)
        self.assertTrue(drafts[0].title.startswith("表格："),
                        "整块都是时段和坐标时，标题要标出来这是张表")

    def test_roster_keeps_full_text_in_detail(self):
        """合成一条之后，整张表要留在 detail 里 —— 不然信息就丢了。"""
        draft = parse(ROSTER, from_file=True)[0]
        self.assertIn("B组", draft.detail)
        self.assertIn("周三", draft.detail)

    def test_roster_from_pasted_text_also_merges(self):
        """粘贴进来的值班表（不是文件）也要合成一条。"""
        self.assertEqual(len(parse(ROSTER, from_file=False)), 1)


class TestDocumentMerging(unittest.TestCase):
    """文件里的内容默认整份一条，除非明确是任务清单。"""

    def test_one_homework_item_merges(self):
        drafts = parse(HOMEWORK_ONE, from_file=True)
        self.assertEqual(len(drafts), 1, "只有一项作业，不该切成三条")
        self.assertEqual(drafts[0].title, "提交实验报告")
        self.assertTrue(drafts[0].due_at.startswith("2026-10-12"), drafts[0].due_at)

    def test_two_homework_items_split(self):
        """真的有两项作业时要拆开 —— 别把整份合成一条。"""
        drafts = parse(HOMEWORK_TWO, from_file=True)
        self.assertEqual(len(drafts), 2)
        titles = sorted(d.title for d in drafts)
        self.assertEqual(titles, ["交数学作业", "提交实验报告"])

    def test_junk_lines_do_not_become_tasks(self):
        """文件里的要求描述、机构抬头不是待办。"""
        for draft in parse(HOMEWORK_ONE, from_file=True):
            self.assertNotIn("2000", draft.title)
            self.assertNotIn("交到", draft.title)
        for draft in parse(POSTER, from_file=True):
            self.assertNotEqual(draft.title, "成都理工大学")

    def test_plain_list_from_paste_still_splits(self):
        """粘贴的短清单不受"文件默认合并"影响。"""
        self.assertEqual(len(parse("买牛奶\n拿快递\n写作业", from_file=False)), 3)


class TestTitleSelection(unittest.TestCase):
    """标题要选最像任务的那行，不能无脑取第一行。"""

    def test_skips_org_header(self):
        draft = parse(POSTER, from_file=True)[0]
        self.assertEqual(draft.title, "学术讲座第82期")

    def test_prefers_the_line_with_a_deadline(self):
        draft = parse(HOMEWORK_ONE, from_file=True)[0]
        self.assertEqual(draft.title, "提交实验报告")

    def test_notice_keeps_its_own_title(self):
        """通知的第一行就是标题，不该被"噪声词"过滤掉。"""
        draft = parse(NOTICE, from_file=False)[0]
        self.assertEqual(draft.title, "关于招募志愿者的通知")

    def test_merged_task_keeps_whole_text(self):
        draft = parse(NOTICE, from_file=False)[0]
        self.assertIn("E1B516", draft.detail)
        self.assertIn("12:00", draft.detail)


class TestSolidLineRule(unittest.TestCase):
    """solid_lines 的判据：有时间线索 + 摘完还有实质内容。"""

    def test_date_only_lines_count_as_solid(self):
        """回归：判据一度要求"必须有钟点"，于是"下周一前提交实验报告"
        被判成"立不住"，一份两项作业的清单被合成了一条。"""
        lines = ["1. 下周一前提交实验报告", "2. 周五前交数学作业"]
        self.assertEqual(len(q.solid_lines(lines)), 2)

    def test_roster_lines_are_not_solid(self):
        """值班表的每一行摘掉时间只剩坐标，立不住。"""
        lines = [line for line in ROSTER.splitlines() if line.strip()]
        self.assertEqual(len(q.solid_lines(lines)), 0)

    def test_plain_lines_are_not_solid(self):
        self.assertEqual(len(q.solid_lines(["买牛奶", "拿快递"])), 0)

    def test_notice_lines_are_not_solid(self):
        """通知里没有一行能独立成事（"时间：…"是字段行）。"""
        lines = [line for line in NOTICE.splitlines() if line.strip()]
        self.assertEqual(len(q.solid_lines(lines)), 0)


class TestMyLines(unittest.TestCase):
    """表里属于自己的那几行单独拎出来。

    用户明确要过这个效果："可以让用户填自己的名称，昵称，
    可以拎出来强调"。整张表合成一条之后，你还得自己在里面找"我周几"。
    """

    ROSTER_WITH_ME = """周一 8:50-12:00 E1B516（A组）
周一 9:50-12:00 E1A504（B组）
周二 13:50-16:20 E1B412（邬思超）
周二 13:50-16:20 C075-05-32（D组）
周三 14:00-16:00 E1B516（E组）"""

    def setUp(self):
        from app.runtime.config import config

        self.config = config
        self.original = list(config.get("parse", "my_names", []) or [])

    def tearDown(self):
        self.config["parse"]["my_names"] = self.original

    def test_my_row_is_pulled_out(self):
        self.config["parse"]["my_names"] = ["邬思超"]
        drafts = parse(self.ROSTER_WITH_ME, from_file=True)
        titles = [d.title for d in drafts]
        self.assertTrue(any(t.startswith("我的：") for t in titles),
                        f"没把属于我的那行拎出来：{titles}")
        mine = [d for d in drafts if d.title.startswith("我的：")][0]
        self.assertIn("E1B412", mine.title)

    def test_table_itself_is_still_kept(self):
        """拎出来的是**额外**一条，整张表不能被顶掉。"""
        self.config["parse"]["my_names"] = ["邬思超"]
        titles = [d.title for d in parse(self.ROSTER_WITH_ME, from_file=True)]
        self.assertTrue(any(t.startswith("表格：") for t in titles), titles)

    def test_no_names_configured_no_extra_task(self):
        self.config["parse"]["my_names"] = []
        drafts = parse(self.ROSTER_WITH_ME, from_file=True)
        self.assertEqual(len(drafts), 1)

    def test_name_not_present_no_extra_task(self):
        self.config["parse"]["my_names"] = ["李四"]
        drafts = parse(self.ROSTER_WITH_ME, from_file=True)
        self.assertEqual(len(drafts), 1)

    def test_plain_list_is_not_affected(self):
        """普通清单不做这件事 —— 每行本来就是独立的事。"""
        self.config["parse"]["my_names"] = ["拿快递"]
        drafts = parse("买牛奶\n拿快递\n写作业", from_file=False)
        self.assertEqual(len(drafts), 3)
        self.assertFalse(any(d.title.startswith("我的：") for d in drafts))


class TestMergeWindowConfig(unittest.TestCase):
    """合并窗口是配置项，不是写死的数字。"""

    def test_window_is_configured(self):
        from app.runtime.config import config

        self.assertEqual(config.get("parse", "merge_window_seconds"), 60)


if __name__ == "__main__":
    unittest.main()
