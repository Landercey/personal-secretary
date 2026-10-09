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
        self.assertEqual(coerce("parse", "confidence_min", "0.5"), 0.5)


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

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_config_key_shows_up_on_the_ui(self):
        """界面是照着 DEFAULTS 生成的 —— 漏一项就等于"这功能没入口"。"""
        shown = {f"{section['name']}.{item['key']}"
                 for section in self.config.schema() for item in section["keys"]}
        expected = {f"{section}.{key}"
                    for section, values in DEFAULTS.items() for key in values}
        self.assertEqual(shown, expected)

    def test_sections_have_chinese_titles_and_are_ordered(self):
        sections = self.config.schema()
        titles = [s["title"] for s in sections[:3]]
        self.assertEqual(titles, ["企业微信推送", "提醒时间", "QQ 接入"])
        for section in sections:
            self.assertTrue(section["title"])

    def test_kind_matches_the_default_value(self):
        kinds = {f"{s['name']}.{i['key']}": i["kind"]
                 for s in self.config.schema() for i in s["keys"]}
        self.assertEqual(kinds["push.times"], "list")
        self.assertEqual(kinds["qq.group_ids"], "list")
        self.assertEqual(kinds["wecom_bot.enabled"], "bool")
        self.assertEqual(kinds["resource.mem_critical_percent"], "int")
        self.assertEqual(kinds["wecom_bot.webhook"], "text")

    def test_every_field_has_a_hint(self):
        """界面上每个框都得有一句人话说明，不然用户只能猜。"""
        missing = [f"{s['name']}.{i['key']}"
                   for s in self.config.schema() for i in s["keys"] if not i["hint"]]
        self.assertEqual(missing, [], f"这些配置项没有说明：{missing}")


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
        self.assertTrue(result["sections"])
        self.assertEqual(result["path"], str(self.path))

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
        self.assertEqual(set(by_key), {"risk", "wecom", "qq", "ai"})
        self.assertFalse(by_key["risk"]["ok"])
        self.assertFalse(by_key["wecom"]["ok"])
        self.assertFalse(state["done"])
        self.assertTrue(by_key["ai"]["optional"])       # AI 是选配

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
        self.assertEqual(api.sync_now(), {"ok": True, "added": 3})


if __name__ == "__main__":
    unittest.main()
