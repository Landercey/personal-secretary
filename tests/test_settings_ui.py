"""界面设置面板的底座：配置能不能被界面读、改、写回去。

这一层最容易出两种事故，所以测试盯着这两条：

  1. **写回把用户的东西冲掉**。config.toml 是用户唯一会亲手改的文件，
     他可能在里面加过自己的注释。写回必须按行改，不能整份重生成。
  2. **类型写错**。界面上传回来的都是字符串，写进 TOML 变成
     `group_ids = ["123"]`（带引号）—— 群号比对永远不相等，
     表现是"我明明填了群，一条都收不到"，最难查。
"""

from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.runtime.config import Config, DEFAULTS, coerce      # noqa: E402
from app.runtime.config import (                              # noqa: E402
    ADVANCED_KEYS, LOCKED_KEYS, PENDING_SECTIONS,
)
from app.ui.bridge import Api                                # noqa: E402

# 必须按模块路径取，不能写 `from app.runtime import config` ——
# app/runtime/__init__.py 里 `config` 这个名字被那个全局 Config 实例占了。
config_module = importlib.import_module("app.runtime.config")

OWN_FILE = """# 这是我自己写的备注，别弄丢
[push]
times = ["07:30"]
# 这行也别弄丢
catch_up_minutes = 120

[qq]
group_ids = [123456]
"""


class TestCoerce(unittest.TestCase):
    def test_group_ids_become_numbers_not_strings(self):
        """带引号的群号会让"这个群要不要收"永远判断为否。"""
        self.assertEqual(coerce("qq", "group_ids", "123456, 789012"),
                         [123456, 789012])
        self.assertEqual(coerce("qq", "group_ids", [111]), [111])

    def test_times_stay_strings(self):
        self.assertEqual(coerce("push", "times", "07:30, 21:30"),
                         ["07:30", "21:30"])

    def test_names_accept_chinese_comma_and_newlines(self):
        self.assertEqual(coerce("parse", "my_names", "张三，小张\n老张"),
                         ["张三", "小张", "老张"])

    def test_empty_list(self):
        self.assertEqual(coerce("qq", "group_ids", ""), [])
        self.assertEqual(coerce("qq", "group_ids", "   "), [])

    def test_bool_from_strings(self):
        for text in ("true", "TRUE", "1", "是", "开"):
            self.assertIs(coerce("wecom_bot", "enabled", text), True)
        for text in ("false", "0", "", "否"):
            self.assertIs(coerce("wecom_bot", "enabled", text), False)
        self.assertIs(coerce("wecom_bot", "enabled", True), True)

    def test_numbers(self):
        self.assertEqual(coerce("resource", "mem_critical_percent", "90"), 90)
        self.assertEqual(coerce("extract", "render_scale", "2.5"), 2.5)


class TestWriteBack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(OWN_FILE, encoding="utf-8")
        self.config = Config.load(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def test_replaces_one_line_and_keeps_user_comments(self):
        self.config.set("push", "times", "09:00, 22:30")
        text = self.text()

        self.assertIn('times = ["09:00", "22:30"]', text)
        self.assertNotIn('times = ["07:30"]', text)
        # 用户自己的注释一个字都不能少
        self.assertIn("# 这是我自己写的备注，别弄丢", text)
        self.assertIn("# 这行也别弄丢", text)
        # 别的段没被碰
        self.assertIn("group_ids = [123456]", text)

    def test_inserts_missing_key_into_existing_section(self):
        """config.toml 可能是旧版本生成的，缺了新加的键。"""
        self.config.set("push", "enabled", False)
        text = self.text()

        self.assertIn("enabled = false", text)
        push_start = text.index("[push]")
        qq_start = text.index("[qq]")
        self.assertLess(push_start, text.index("enabled = false"), qq_start)

    def test_appends_missing_section(self):
        self.config.set("timetable", "url", "https://example.com/a.ics")
        text = self.text()

        self.assertIn("[timetable]", text)
        self.assertIn('url = "https://example.com/a.ics"', text)
        self.assertIn("# 这是我自己写的备注，别弄丢", text)

    def test_written_value_is_readable_after_reload(self):
        """写回去的东西，重新读一遍必须是新值（不然界面显示的和实际用的会不一致）。"""
        self.config.set("qq", "group_ids", "111, 222")
        fresh = Config.load(self.path)
        self.assertEqual(fresh.get("qq", "group_ids"), [111, 222])

    def test_unknown_key_is_refused(self):
        with self.assertRaises(KeyError):
            self.config.set("push", "不存在的项", 1)
        with self.assertRaises(KeyError):
            self.config.set("不存在的段", "x", 1)

    def test_set_only_touches_its_own_section(self):
        before = self.text()
        self.config.set("wecom_bot", "webhook", "https://qyapi.example/hook")
        text = self.text()

        self.assertIn('webhook = "https://qyapi.example/hook"', text)
        # [qq] 那几行原样保留
        self.assertIn("group_ids = [123456]", text)
        self.assertIn(OWN_FILE.strip().splitlines()[0], before)


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config.load(Path(self.tmp.name) / "config.toml")
        self.groups = self.config.schema()
        self.fields = {f"{sec['name']}.{item['key']}": item
                       for group in self.groups
                       for sec in group["sections"]
                       for item in sec["keys"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_config_key_shows_up_on_the_ui(self):
        """界面是照着 DEFAULTS 生成的 —— 漏一项就等于"这功能没入口"。

        唯一的例外是**还没做的段**：那些键在界面上故意不给输入框
        （填了不生效比没有更伤信任），只留一句"还没做"。
        """
        expected = {f"{section}.{key}"
                    for section, values in DEFAULTS.items() for key in values}
        waiting = {f"{name}.{key}" for name in PENDING_SECTIONS
                   for key in DEFAULTS.get(name, {})}
        self.assertEqual(set(self.fields) | waiting, expected)

    def test_pending_sections_show_a_note_and_no_inputs(self):
        """AI 还没做 —— 界面上一个框都不该有，但得说一句人话。"""
        shown = {s["name"]: s for g in self.groups for s in g["sections"]
                 if s.get("pending")}
        self.assertEqual(set(shown), set(PENDING_SECTIONS))
        for name, section in shown.items():
            self.assertEqual(section["keys"], [], f"{name} 不该有输入框")
            self.assertGreater(len(section["pending"]), 10)   # 说人话，不是"未实现"

    def test_finished_sections_are_not_marked_pending(self):
        for group in self.groups:
            for section in group["sections"]:
                if section["name"] not in PENDING_SECTIONS:
                    self.assertEqual(section["pending"], "")

    def test_settings_are_grouped_into_tabs(self):
        """用户的原话是"太杂，没有分类"。四个标签页，每页 2~3 段。"""
        self.assertEqual([g["title"] for g in self.groups],
                         ["推送到手机", "收消息", "看懂消息", "数据与资源"])
        for group in self.groups:
            self.assertTrue(group["desc"])
            # 一页最多四段。再多就该拆新组了 —— 用户的原话是"太杂，没有分类"。
            self.assertTrue(1 <= len(group["sections"]) <= 4, group["title"])
            for section in group["sections"]:
                self.assertTrue(section["title"])

    def test_kind_matches_the_default_value(self):
        self.assertEqual(self.fields["push.times"]["kind"], "list")
        self.assertEqual(self.fields["qq.group_ids"]["kind"], "list")
        self.assertEqual(self.fields["wecom_bot.enabled"]["kind"], "bool")
        self.assertEqual(self.fields["resource.mem_critical_percent"]["kind"], "int")
        self.assertEqual(self.fields["wecom_bot.webhook"]["kind"], "text")

    def test_every_field_has_a_hint(self):
        """界面上每个框都得有一句人话说明，不然用户只能猜。"""
        missing = [path for path, item in self.fields.items() if not item["hint"]]
        self.assertEqual(missing, [], f"这些配置项没有说明：{missing}")

    def test_advanced_fields_are_marked_and_sorted_last(self):
        """高级项要在段内排到最后（界面上它们被折进一个收起的小块）。"""
        marked = {path for path, item in self.fields.items()
                  if item["level"] == "advanced"}
        # 还没做的段没有输入框，也就没有"高级"可言
        waiting = {f"{name}.{key}" for name in PENDING_SECTIONS
                   for key in DEFAULTS.get(name, {})}
        self.assertEqual(marked, set(ADVANCED_KEYS) - waiting)

        for group in self.groups:
            for section in group["sections"]:
                levels = [item["level"] for item in section["keys"]]
                self.assertEqual(levels, sorted(levels, key=lambda x: x == "advanced"),
                                 f"{section['name']} 的字段顺序不对")

    def test_connection_settings_are_locked_with_a_reason(self):
        """监听地址/端口/路径/令牌由程序写进 NapCat 配置。

        用户在这儿单方面改，两边对不上，QQ 会**静默收不到消息** ——
        所以这几项必须默认锁住，而且要说清楚为什么。
        """
        locked = {path for path, item in self.fields.items() if item["locked"]}
        self.assertEqual(locked, set(LOCKED_KEYS))
        for path in ("qq.bind_host", "qq.bind_port", "qq.path", "qq.token"):
            self.assertIn(path, locked)
            self.assertGreater(len(self.fields[path]["locked"]), 10)   # 说人话，不是"不可改"

    def test_ordinary_fields_are_not_locked(self):
        """别锁过头：群号、推送时间、Webhook 这些用户本来就要改。"""
        for path in ("qq.group_ids", "push.times", "wecom_bot.webhook",
                     "timetable.url"):
            self.assertFalse(self.fields[path]["locked"], path)


class TestBridge(unittest.TestCase):
    """界面能调到的几个新方法。用临时配置，不碰真 config.toml。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(OWN_FILE, encoding="utf-8")
        self.config = Config.load(self.path)
        self._original = config_module.config
        config_module.config = self.config
        self.api = Api(None)          # 这几个方法只碰配置，不碰数据库

    def tearDown(self):
        config_module.config = self._original
        self.tmp.cleanup()

    def test_settings_lists_sections_and_path(self):
        result = self.api.settings()
        self.assertTrue(result["groups"])
        self.assertEqual(result["path"], str(self.path))
        # 分组是给界面做标签页用的，每组都得有中文名
        self.assertTrue(all(g["title"] for g in result["groups"]))

    def test_save_settings_writes_through(self):
        result = self.api.save_settings({"push.times": "06:30, 22:00",
                                         "qq.group_ids": "123456, 654321"})
        self.assertEqual(sorted(result["changed"]),
                         ["push.times", "qq.group_ids"])
        fresh = Config.load(self.path)
        self.assertEqual(fresh.get("push", "times"), ["06:30", "22:00"])
        self.assertEqual(fresh.get("qq", "group_ids"), [123456, 654321])

    def test_setup_state_reports_what_is_missing(self):
        state = self.api.setup_state()
        by_key = {item["key"]: item for item in state["checks"]}
        # AI 不在清单里 —— 它还没做，摆一条"配了 AI"等于催用户去填一个不生效的框
        self.assertEqual(set(by_key), {"risk", "wecom", "qq"})
        self.assertFalse(by_key["risk"]["ok"])
        self.assertFalse(by_key["wecom"]["ok"])
        self.assertFalse(state["done"])

    def test_confirm_risk_remembers(self):
        self.api.confirm_risk()
        self.assertTrue(Config.load(self.path).get("risk", "confirmed_secondary_account"))
        risk = next(c for c in self.api.setup_state()["checks"] if c["key"] == "risk")
        self.assertTrue(risk["ok"])

    def test_setup_done_once_the_two_required_things_are_filled(self):
        self.api.confirm_risk()
        self.api.save_settings({"wecom_bot.webhook": "https://x/y",
                                "qq.group_ids": "123456"})
        self.assertTrue(self.api.setup_state()["done"])

    def test_add_text_goes_through_the_injected_action(self):
        """界面不自己 import 理解层，动作是 run.py 递进来的。"""
        seen = []
        api = Api(None, actions={"add_text": lambda text: seen.append(text) or 1})
        self.assertEqual(api.add_text("明天下午三点交作业"), {"ok": True, "added": 1})
        self.assertEqual(seen, ["明天下午三点交作业"])

    def test_add_text_refuses_empty(self):
        api = Api(None, actions={"add_text": lambda text: 1})
        self.assertFalse(api.add_text("   ")["ok"])

    def test_missing_action_says_so_instead_of_crashing_silently(self):
        with self.assertRaises(RuntimeError):
            Api(None).add_text("明天下午三点交作业")

    def test_sync_now_uses_the_injected_action(self):
        api = Api(None, actions={"sync": lambda: 3})
        self.assertEqual(api.sync_now(), {"ok": True, "added": 3, "notes": []})


class _FakeStore:
    """只用到 recent_pushes 一个方法，别为了测一条建议去开真数据库。"""

    def __init__(self, pushes):
        self._pushes = pushes

    def recent_pushes(self, limit: int = 20):
        return self._pushes[:limit]


class TestPushTimeSuggestion(unittest.TestCase):
    """**什么时候让用户去设推送时间** —— 这是本轮要解决的问题。

    答案不是"首次启动就问"：那会儿用户还没收到过任何推送，
    不知道这东西长什么样，让他选时间等于让他凭空决定，他只会乱点。
    要**等第一条推送真的到了手机上**再问，这时候他才有判断依据。

    "已经问过没有"不用存标记：推送时间还是默认值 = 他没改过 = 继续提醒；
    他改了，这条建议自己就消失了。一个状态都不用存。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(OWN_FILE, encoding="utf-8")
        self._original = config_module.config

    def tearDown(self):
        config_module.config = self._original
        self.tmp.cleanup()

    def use_config(self, times=None):
        values = {"push": {"times": times}} if times else {}
        config_module.config = Config(values, self.path)

    def test_no_suggestion_before_the_first_push(self):
        self.use_config()
        api = Api(_FakeStore([]))
        self.assertIsNone(api.setup_state()["suggestion"])

    def test_suggestion_appears_after_a_successful_push(self):
        self.use_config()
        api = Api(_FakeStore([{"ok": 1}]))
        suggestion = api.setup_state()["suggestion"]

        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["section"], "push")   # 点一下能跳到对应设置
        self.assertIn("07:30", suggestion["text"])        # 得把当前值告诉他才谈得上改

    def test_failed_push_does_not_count(self):
        """推失败和推成功是两回事：没收到东西就谈不上"想换时间"。"""
        self.use_config()
        self.assertIsNone(Api(_FakeStore([{"ok": 0}])).setup_state()["suggestion"])

    def test_suggestion_disappears_once_he_changed_the_times(self):
        self.use_config(times=["06:30", "22:00"])
        self.assertIsNone(Api(_FakeStore([{"ok": 1}])).setup_state()["suggestion"])

    def test_no_store_is_not_an_error(self):
        """有些地方（比如测试、或者界面单独起）没有 store，不能因此炸。"""
        self.use_config()
        self.assertIsNone(Api(None).setup_state()["suggestion"])

    def test_sync_carries_notes_back_to_the_ui(self):
        """同步时的提醒必须能传回界面 —— 只 print 的话界面用户看不到。"""
        api = Api(None, actions={"sync": lambda: {
            "added": 2, "notes": ["课表里有单双周但没填开学第一周"]}})
        result = api.sync_now()

        self.assertEqual(result["added"], 2)
        self.assertEqual(result["notes"], ["课表里有单双周但没填开学第一周"])

    def test_sync_tolerates_the_old_int_return(self):
        api = Api(None, actions={"sync": lambda: 3})
        self.assertEqual(api.sync_now(), {"ok": True, "added": 3, "notes": []})


class TestFullwidthPunctuation(unittest.TestCase):
    """中文输入法下打出来的全角标点，不能把整份配置打回默认值。

    用户报的原话是"设置的逗号能不能是中文输入法的逗号"。查下来界面那侧
    本来就认（coerce 同时按半角/全角切），**真正会出事的是 config.toml**：

        group_ids = [123456，789012]

    整份文件解析失败 → 所有设置静默退回默认值：webhook 没了、群号空了。
    用户看到的现象是"我就打了个逗号，怎么设置全丢了"。
    """

    def load(self, text: str) -> Config:
        folder = tempfile.mkdtemp(prefix="ps-fw-")
        path = Path(folder) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return Config.load(path)

    def test_fullwidth_comma_in_a_list_is_tolerated(self):
        cfg = self.load('[qq]\ngroup_ids = [123456，789012]\n')

        self.assertEqual(cfg.get("qq", "group_ids"), [123456, 789012])
        self.assertEqual(cfg.load_error, "")          # 不再谎报"读不了"
        self.assertIn("全角", cfg.load_note)          # 但要说一声

    def test_fullwidth_syntax_does_not_wipe_other_settings(self):
        """这才是不纠的可怕之处：一个逗号，把 webhook 也带走了。"""
        cfg = self.load(
            '[qq]\ngroup_ids = [1，2]\n\n'
            '[wecom_bot]\nwebhook = "https://example/hook"\nenabled = true\n')

        self.assertEqual(cfg.get("qq", "group_ids"), [1, 2])
        self.assertEqual(cfg.get("wecom_bot", "webhook"), "https://example/hook")
        self.assertTrue(cfg.get("wecom_bot", "enabled"))

    def test_fullwidth_equals_sign(self):
        """全角等号也是中文输入法常打出来的。"""
        cfg = self.load('[qq]\naccount ＝ 123456789\n')
        self.assertEqual(cfg.get("qq", "account"), 123456789)

    def test_good_file_records_no_note(self):
        cfg = self.load('[qq]\ngroup_ids = [1, 2]\n')
        self.assertEqual(cfg.load_note, "")
        self.assertEqual(cfg.load_error, "")

    def test_really_broken_file_still_falls_back(self):
        """别把"纠标点"变成"什么都能收" —— 真写坏的还得退回默认值。"""
        cfg = self.load('[qq\ngroup_ids = [1, 2\n')
        self.assertTrue(cfg.load_error)
        self.assertEqual(cfg.get("qq", "group_ids"), [])

    def test_ui_side_already_accepts_chinese_commas(self):
        for section, key in (("qq", "group_ids"), ("qq", "inbox_ids"),
                             ("push", "times"), ("parse", "my_names")):
            with self.subTest(key=key):
                self.assertEqual(len(coerce(section, key, "a，b")), 2)


if __name__ == "__main__":
    unittest.main()
