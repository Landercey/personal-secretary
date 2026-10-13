"""第 2 步门禁：规则解析的最小正确性。

重点不是"能解析出东西"，而是**第 6 步要达标的两个数字在这里先立住**：
  1. 碎片（摘掉时间和编号后什么都不剩）不许变成待办；
  2. 短待办（"拿快递""打游戏"）一条都不许误伤。
这两条一破，后面的准确率指标就没意义了。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.pipeline import quick_parse                        # noqa: E402
from app.pipeline.text_norm import to_simplified            # noqa: E402


class TestFragments(unittest.TestCase):
    """碎片必须被挡掉。这些都是从真实库里捞出来的原话。"""

    FRAGMENTS = [
        "10月9日-10月21日 12:20-13:50，17:50-18:50",   # 纯时段表，旧项目标题变成它自己
        "10月8日8:30-18:00",
        "（A组）", "（B组）", "（C组）", "（D组）", "（E组）",
        "📅周一", "📅周二",
        "5G", "905G 5G",                                # 截图里的信号栏
        "[图片]",
        "周五之前",
        "E1B516（A组）",                                 # 教室号 + 组别，不是一件事
        "[CQ:flashtransfer,fileSetId=a20b234a-2504-43aa-ba8e-9a3138c68ab0]",
        "e2a404", "cdutjwtw@163.com", "https://www.aao.cdut.edu.cn/",
        "玉0.0.0.0",                                     # OCR 认出来的乱码
    ]

    def test_fragments_never_become_todos(self):
        for text in self.FRAGMENTS:
            with self.subTest(text=text):
                self.assertIsNone(quick_parse.parse_line(text),
                                  f"碎片不该变成待办：{text}")

    def test_is_fragment_agrees(self):
        for text in self.FRAGMENTS:
            with self.subTest(text=text):
                self.assertTrue(quick_parse.is_fragment(text))

    def test_title_never_falls_back_to_raw_text(self):
        """根因回归：标题为空时不许把原文照抄回去。

        旧项目就是这么干的，于是纯时段表的标题变成了它自己 ——
        用户看到的就是"经常只有时间"。
        """
        self.assertEqual(quick_parse._build_title("10月9日-10月21日 12:20-13:50"), "")

    def test_cq_code_inside_a_real_message_is_stripped(self):
        """CQ 代码夹在真消息里时要剥掉，不能污染标题。"""
        draft = quick_parse.parse_line("[CQ:at,qq=123456] 明天下午三点交作业")
        self.assertIsNotNone(draft)
        self.assertEqual(draft.title, "交作业")


class TestShortTodosPreserved(unittest.TestCase):
    """短待办零误伤。这是自己随手记的，一条都不许丢。

    注意：这里**故意不含**"学号""格式" —— 它们是通知里的字段名，不是事情。
    旧库里它们各自成了一条待办，那是误判，不是要保护的对象。
    见下面 TestFieldLabels。
    """

    KEEP = ["拿快递", "取快递", "打游戏", "耍起", "买牛奶", "交作业"]

    def test_short_todos_survive(self):
        for text in self.KEEP:
            with self.subTest(text=text):
                draft = quick_parse.parse_line(text)
                self.assertIsNotNone(draft, f"短待办被误伤了：{text}")
                self.assertEqual(draft.title, text)


class TestFieldLabels(unittest.TestCase):
    """通知里的字段名本身不是一件事。

    旧项目实测踩过：接龙消息里的"截止时间：10月3日 07:41"被记成了待办「截止时间」。
    "学号""格式"同理 —— 它们是表单栏名，不是要做的事。
    """

    FIELD_LABELS = ["学号", "格式", "姓名", "备注", "主题", "名单"]

    def test_bare_field_labels_are_not_todos(self):
        for text in self.FIELD_LABELS:
            with self.subTest(text=text):
                self.assertIsNone(quick_parse.parse_line(text),
                                  f"字段名不该变成待办：{text}")

    def test_field_line_forms_are_not_todos(self):
        for text in ("截止时间：10月3日 07:41", "联系人：李老师", "集中办理时间：周五"):
            with self.subTest(text=text):
                self.assertIsNone(quick_parse.parse_line(text))

    def test_but_real_short_action_with_field_word_survives(self):
        """"交作业"里带"作业"不是字段名，必须照样收。"""
        draft = quick_parse.parse_line("交作业")
        self.assertIsNotNone(draft)
        self.assertEqual(draft.title, "交作业")


class TestTimeParsing(unittest.TestCase):
    def test_roster_with_a_header_line_is_one_todo(self):
        """值班表第一行是标题时，整张表要合成**一条**，不能拆成人名。

        2026-10-13 离线模拟里真复现了：那张表长这样 ——

            下周值班表
            张三 周一 8:00-12:00
            李四 周二 8:00-12:00

        原来要求"每一行都带时间"，标题那行没有 → 整条规则不成立 →
        被拆成「张三」「李四」两条待办。而"张三"这种两字人名和
        "拿快递"一样长，光看长度分不出是不是待办，所以只能靠
        **标题里的"值班表/排班表"这类词**来判（见 should_merge）。
        """
        block = "下周值班表\n张三 周一 8:00-12:00\n李四 周二 8:00-12:00"
        draft = quick_parse.parse_line(block)
        self.assertIsNotNone(draft)
        self.assertIn("值班表", draft.title)
        self.assertNotEqual(draft.title.strip(), "张三")

    def test_a_list_of_real_todos_still_splits(self):
        """反面：两行各自都是真待办时**不许**当成表合起来（别被上一条规则误伤）。"""
        self.assertFalse(quick_parse.should_merge(["明天交作业", "后天开会"],
                                                  from_file=False))
        self.assertTrue(quick_parse.should_merge(
            ["下周值班表", "张三 周一 8:00-12:00", "李四 周二 8:00-12:00"],
            from_file=False))

    def test_spaces_inside_a_date_are_tolerated(self):
        """数字和「月/日/号」之间带空格也要认（用户真发过这么一条）。

        2026-10-13：作者发「10 月 14 号教学楼 612 下午 1 点半领交表」，
        日期没认出来 → **退回今天** → 提醒时间错了一整天。
        比不认更糟：用户以为程序算得准。手机输入法和微信复制粘贴很容易带空格。
        """
        for text in ("10 月 14 号下午 1 点半领交表",
                     "10月14号下午1点半领交表"):
            with self.subTest(text=text):
                draft = quick_parse.parse_line(text)
                self.assertIsNotNone(draft)
                self.assertTrue(draft.due_at.endswith("13:30:00"), draft.due_at)
        # 带不带空格必须解析成**同一天**（这条不依赖今天是几号）。
        # resolve_date 返回 (时刻, 命中的原文)，所以只比时刻那半边。
        spaced = quick_parse.resolve_date("10 月 14 号")
        tight = quick_parse.resolve_date("10月14号")
        self.assertIsNotNone(spaced)
        self.assertEqual(spaced[0], tight[0])

    def test_relative_day_and_clock(self):
        draft = quick_parse.parse_line("明天下午三点交作业")
        self.assertIsNotNone(draft)
        self.assertEqual(draft.title, "交作业")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertTrue(draft.due_at.startswith(tomorrow), draft.due_at)
        self.assertTrue(draft.due_at.endswith("15:00:00"), draft.due_at)

    def test_day_without_clock_defaults_to_end_of_day(self):
        """只说了哪天没说几点 → 当天 23:59，不是 09:00。

        写 09:00 的话，一到九点这条就变"逾期"标红，可用户说的明明是"今天"。
        """
        draft = quick_parse.parse_line("明天交作业")
        self.assertIsNotNone(draft)
        self.assertTrue(draft.due_at.endswith("23:59:00"), draft.due_at)

    def test_weekday(self):
        draft = quick_parse.parse_line("下周一上午九点开会")
        self.assertIsNotNone(draft)
        self.assertEqual(draft.title, "开会")
        self.assertTrue(draft.due_at.endswith("09:00:00"), draft.due_at)
        self.assertIsNotNone(quick_parse.resolve_date("下周一"))

    def test_location(self):
        draft = quick_parse.parse_line("明天下午3点在图书馆门口集合")
        self.assertIsNotNone(draft)
        self.assertEqual(draft.title, "集合")
        self.assertEqual(draft.location, "图书馆门口")

    def test_traditional_chinese_normalized(self):
        """繁简归一：不归一，繁体里的"點"认不出来，时间会全错。"""
        self.assertEqual(to_simplified("明天早上11點打遊戲"), "明天早上11点打游戏")

    def test_pm_period_applies_to_colon_form_too(self):
        """回归：`下午3:00` 一度被解析成 **03:00**（凌晨三点）。

        原因：`HH:MM` 那条正则没带"上午/下午"前缀，而"3点"那种写法带了 ——
        同一个意思两种写法，一个对一个错，最难发现。
        """
        cases = {
            "下午3:00": (15, 0),
            "下午 3:00": (15, 0),
            "晚上7:30": (19, 30),
            "上午9:00": (9, 0),
            "凌晨1:00": (1, 0),
            "中午12:30": (12, 30),
        }
        for text, (hour, minute) in cases.items():
            with self.subTest(text=text):
                found = quick_parse.resolve_time(text)
                self.assertIsNotNone(found, f"没认出时间：{text}")
                self.assertEqual((found[0], found[1]), (hour, minute))

    def test_colon_and_dian_agree(self):
        """"下午3:00"和"下午3点"必须给出同一个时刻。"""
        colon = quick_parse.resolve_time("下午3:00")
        dian = quick_parse.resolve_time("下午3点")
        self.assertEqual((colon[0], colon[1]), (dian[0], dian[1]))


class TestNoise(unittest.TestCase):
    def test_noise_lines_rejected(self):
        for text in ("", "  ", "---", "=====", "1.", "（2）", "2026-10-08"):
            with self.subTest(text=text):
                self.assertIsNone(quick_parse.parse_line(text))

    def test_greeting_prefix_stripped(self):
        draft = quick_parse.parse_line("各位同学：明天下午3点交作业")
        self.assertIsNotNone(draft)
        self.assertEqual(draft.title, "交作业")


class TestProseDetection(unittest.TestCase):
    def test_notice_paragraph_becomes_one_draft(self):
        """一整段通知不该被逐行切成十几条 —— 旧项目实测切成 14 条。"""
        notice = "\n".join([
            "关于招募志愿者的通知",
            "各位同学：",
            "本周末开展志愿活动，具体安排如下：",
            "时间：10月11日 8:50-12:00",
            "地点：E1B516",
            "分组：A组 B组 C组",
            "请有意向的同学在群里回复",
            "谢谢大家",
        ])
        self.assertTrue(quick_parse.is_prose_block(
            [line for line in notice.splitlines() if line.strip()]
        ))

    def test_short_clean_list_stays_split(self):
        """短清单还是要一行一条，别把正常清单也合成一条。"""
        drafts = quick_parse.parse_text("买牛奶\n拿快递\n写作业", strict=False)
        self.assertEqual(len(drafts), 3)


if __name__ == "__main__":
    unittest.main()
