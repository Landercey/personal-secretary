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

    def test_dangling_connective_left_by_the_removed_time(self):
        """时间词被摘走后，不许留半截「推迟到，」。

        2026-10-16 用户看到的标题："军训时间暂时推迟到，到时候看天气…" ——
        原话是"推迟到**十点**"，十点被摘走后只剩个光杆"到"。
        修法：紧挨着介词的这种时间干脆不摘，标题保留原句，截止时间照算。
        """
        draft = quick_parse.parse_line("军训时间暂时推迟到十点，到时候看天气再具体通知")
        self.assertIsNotNone(draft)
        self.assertNotIn("到，", draft.title)
        self.assertIn("推迟到10点，", draft.title)
        self.assertIn("10:00:00", draft.due_at)

    def test_a_real_word_ending_in_dao_is_kept(self):
        """反例："签到"是真词，不能因为末尾有个"到"就砍掉那个字。"""
        self.assertEqual(quick_parse._build_title("每天签到"), "每天签到")

    def test_cq_code_inside_a_real_message_is_stripped(self):
        """CQ 代码夹在真消息里时要剥掉，不能污染标题。"""
        draft = quick_parse.parse_line("[CQ:at,qq=123456] 明天下午三点交作业")
        self.assertIsNotNone(draft)
        self.assertEqual(draft.title, "交作业")

    def test_at_all_member_leaves_no_stray_member(self):
        """「@全体成员」是完整称呼，标题里不许剩一个孤零零的"成员"。

        2026-10-16 用户报的那条（#44：截图 OCR + 正文一起解析）：标题原来是
        "成员同学们好！…" —— "@全体"被当占位符摘掉，"成员"留下了。
        """
        draft = quick_parse.parse_line("@全体成员   同学们好！今天分享一份《AI科研入门指南》。")
        self.assertIsNotNone(draft)
        self.assertTrue(draft.title.startswith("同学们好"), draft.title)


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
        # 只比**日期**：没说几点时返回的是"现在"，两次调用差几微秒，
        # 比完整时间会在跨微秒时偶尔红一次（2026-10-17 真闪过一次）。
        self.assertEqual(spaced[0].date(), tight[0].date())

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

    def test_bare_minutes_and_a_distant_pm_word(self):
        """「今天下午拉练，2点20前集合」：分钟要认，下午隔着半句话也要生效。

        用户 2026-10-18 报的原话：这条被识别成 02:00（凌晨两点），
        而且标题开头还挂着"下午"。
        """
        draft = quick_parse.parse_line(
            "今天下午拉练，2点20前集合，2点25整队完毕，超过25以后到的同学按缺勤处理")
        self.assertIsNotNone(draft)
        self.assertTrue(draft.due_at.endswith("14:20:00"), draft.due_at)
        self.assertTrue(draft.title.startswith("拉练"), draft.title)

        # 前面没有"下午"就不该硬加 12 小时
        plain = quick_parse.parse_line("2点20前集合")
        self.assertTrue(plain.due_at.endswith("02:20:00"), plain.due_at)

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

    def test_dot_dates_are_understood(self):
        """学校通知里几乎都写「10.18」而不是「10月18日」。"""
        date, _raw = quick_parse.resolve_date("10.18日前交作业")
        self.assertIsNotNone(date)
        self.assertEqual((date.month, date.day), (10, 18))

    def test_a_dot_range_takes_the_end_day(self):
        """「10.11-10.23期间」的截止是后面那天 —— 区间是窗口，窗口到那天关。"""
        date, _raw = quick_parse.resolve_date("学生手册重考——10.11-10.23期间")
        self.assertIsNotNone(date)
        self.assertEqual((date.month, date.day), (10, 23))

    def test_decimal_numbers_are_not_dates(self):
        """「6.5分」「3.5元」不能当日期 —— 否则成绩、钱都会被算成某月某日。"""
        for text in ("这次考试6.5分", "一共3.5元", "1.5小时"):
            with self.subTest(text=text):
                self.assertIsNone(quick_parse.resolve_date(text)[0])

    def test_a_structured_notice_does_not_title_itself_immediately_execute(self):
        """「①.②.」那种通知，标题不能再退化成开头的「✨立即执行」。

        2026-10-17 用户报的：整条通知只剩一个「✨立即执行」当标题，
        真正的内容（学生手册重考、理论学习和考试）全埋在正文里。
        """
        notice = "\n".join([
            "@全体  ",
            "✨立即执行：",
            "请所有同学落实以下内容，注意截止时间和截图标准：",
            "①.学生手册重考（要求95+）——10.11-10.23期间",
            "②.理论学习和考试——（10.18日前）本周内落实成绩截图",
            "军训为必修课程，未参加考试的同学没有学分。",
        ])
        drafts = quick_parse.parse_text(notice)
        self.assertEqual(len(drafts), 1)
        self.assertIn("学生手册重考", drafts[0].title)
        self.assertNotIn("立即执行", drafts[0].title)
        self.assertIn("2026-10-23", drafts[0].due_at)


class TestNoise(unittest.TestCase):
    def test_noise_lines_rejected(self):
        for text in ("", "  ", "---", "=====", "1.", "（2）", "2026-10-08"):
            with self.subTest(text=text):
                self.assertIsNone(quick_parse.parse_line(text))

    def test_greeting_prefix_stripped(self):
        draft = quick_parse.parse_line("各位同学：明天下午3点交作业")
        self.assertIsNotNone(draft)
        self.assertEqual(draft.title, "交作业")

    def test_chinese_clock_keeps_a_short_notice_alive(self):
        """「接营长通知，军训时间暂时推迟到十点半」不能被当寒暄扔掉。

        2026-10-16：这句整条丢了（别人的阿拉伯数字写法却进得来）—— 噪声判定
        看的是中文数字原文，认不出"十点半"是时间，就当成标题类寒暄。
        """
        draft = quick_parse.parse_line("接营长通知，军训时间暂时推迟到十点半")
        self.assertIsNotNone(draft)
        self.assertIn("10:30:00", draft.due_at)


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

    def test_a_document_with_few_task_lines_becomes_one_draft(self):
        """文件里"像待办的句子"不过半 → 整份合成一条（2026-10-17 实测的 9 条碎片）。

        真清单一一行一件事，占比不会低于一半；通知/说明文档才会。
        """
        doc = "\n".join([
            "成都理工大学 2026 级本科新生爱国主义、国防常识、安全教育",
            "学校通过动画视频、在线试题等多媒体手段开展在线学习。",
            "学习时间由 2026 年 10 月 10 日起，截止日期以另行通知为准。",
            "请同学们注意学习考试截止时间，考试要求为 60 分及格。",
            "1. 微信公众号搜索并关注平安成理公众号",
            "2. 点击安全教育选项中的新生安全教育选项进入",
            "3. 输入姓名和学号，点击登录",
            "微课学习：点击进入课程学习视频，学习期间不可以快进。",
            "试题学习，为各章节考试试题以及正确答案学习。",
            "点击去考试后进入考试计划，考试时间为 40 分钟。",
        ])
        drafts = quick_parse.parse_text(doc, strict=True, from_file=True)
        self.assertEqual(len(drafts), 1, [d.title for d in drafts])

    def test_a_real_checklist_file_stays_split(self):
        """反过来：一行一件事的清单文件不能被合成一条。"""
        checklist = "\n".join([
            "10月20日前交实验报告",
            "10月21日前交课程论文",
            "10月22日前把学生证交给班长",
        ])
        drafts = quick_parse.parse_text(checklist, strict=True, from_file=True)
        self.assertEqual(len(drafts), 3, [d.title for d in drafts])

    def test_titled_numbered_block_becomes_one_draft(self):
        """「标题 + 编号子项」是**一份东西**，不是三件事。

        用户原话："它识别到的那个标题明显是错误的"。实测这段切出来是
        「交到」「字数不少于 2000 字」两条，谁看都不是待办。
        """
        drafts = quick_parse.parse_text(
            "实验报告要求\n1. 下周一前提交实验报告\n2. 字数不少于 2000 字\n"
            "3. 交到实验楼302", strict=False)

        self.assertEqual([draft.title for draft in drafts], ["提交实验报告"])
        self.assertEqual(drafts[0].location, "实验楼302")     # 地点别丢
        self.assertIsNotNone(drafts[0].due_at)                 # 时间也别丢

    def test_numbered_list_without_a_title_stays_split(self):
        """真清单第一行自己就带编号 —— 没有标题行，照旧一行一条。"""
        drafts = quick_parse.parse_text("1. 交作业\n2. 买牛奶", strict=False)

        self.assertEqual(len(drafts), 2)


if __name__ == "__main__":
    unittest.main()
