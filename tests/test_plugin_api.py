"""M3：插件微内核 —— 清单、接口版本、插件自己的设置页。

目标：社区大佬能自己写插件、自己带设置，**不用改主程序一行前端**，
而且写坏了不拖垮别人。
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.runtime import plugins as plugin_module                  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = self.root / "plugins"
        self.folder.mkdir()
        self._old = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = str(self.root)
        # 插件加载会往全局 DEFAULTS / PLUGIN_SECTIONS 里注册设置段 ——
        # 测试之间必须还原，否则后面的"设置分组"检查会多出一个组（真的踩到）。
        # 要的是模块（含 DEFAULTS），不是 `app.runtime.config` 这个包属性 ——
        # 包里的 `config` 名字已经被单例覆盖了，只能从 sys.modules 拿模块。
        self._cfg = sys.modules["app.runtime.config"]
        self._snap = (dict(self._cfg.PLUGIN_SECTIONS),
                      {k: dict(v) for k, v in self._cfg.DEFAULTS.items()},
                      dict(self._cfg.KEY_COMMENTS))

    def tearDown(self):
        sections, defaults, comments = self._snap
        self._cfg.PLUGIN_SECTIONS.clear()
        self._cfg.PLUGIN_SECTIONS.update(sections)
        self._cfg.DEFAULTS.clear()
        self._cfg.DEFAULTS.update(defaults)
        self._cfg.KEY_COMMENTS.clear()
        self._cfg.KEY_COMMENTS.update(comments)
        if self._old is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old
        self.tmp.cleanup()

    def write(self, name: str, body: str) -> Path:
        path = self.folder / f"{name}.py"
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def load(self) -> plugin_module.Registry:
        registry = plugin_module.Registry()
        plugin_module.load(self.root, registry)
        return registry


class TestManifest(Base):
    def test_a_plugin_can_declare_version_and_permissions(self):
        self.write("hello", """
            TITLE = "打个招呼"
            PLUGIN = {"api": 1, "version": "2.3.4", "permissions": ["network"]}
            def register(registry):
                pass
        """)
        status = self.load().status[0]
        self.assertTrue(status.ok, status.error)
        self.assertEqual(status.version, "2.3.4")
        self.assertEqual(status.permissions, ("network",))

    def test_a_plugin_wanting_a_newer_api_is_refused_clearly(self):
        self.write("future", """
            TITLE = "未来的插件"
            PLUGIN = {"api": 99, "version": "1.0"}
            def register(registry):
                pass
        """)
        # 另一个正常插件必须照常加载（坏一个不影响别人）
        self.write("good", """
            TITLE = "正常的"
            def register(registry):
                pass
        """)
        registry = self.load()
        by_name = {s.name: s for s in registry.status}
        self.assertFalse(by_name["future"].ok)
        self.assertIn("升级主程序", by_name["future"].error)
        self.assertTrue(by_name["good"].ok)

    def test_a_plugin_without_a_manifest_still_works(self):
        """老插件（没写 PLUGIN）必须继续能用 —— 接口只能加、不能切。"""
        self.write("old_style", """
            TITLE = "老插件"
            def register(registry):
                pass
        """)
        status = self.load().status[0]
        self.assertTrue(status.ok)
        self.assertEqual(status.version, "")


class TestPluginSettings(Base):
    def test_settings_from_the_manifest_show_up_in_the_settings_schema(self):
        """插件声明的设置段要自动出现在设置面板的「插件」组里。"""
        self.write("with_settings", """
            TITLE = "带设置的"
            PLUGIN = {
                "api": 1, "version": "1.0",
                "settings": {"mypref": {
                    "title": "我的偏好",
                    "desc": "试试",
                    "fields": {"words": "a,b"},
                    "comments": {"words": "关键词\\n#   用逗号分开"},
                }},
            }
            def register(registry):
                pass
        """)
        self.load()

        from app.runtime.config import config

        groups = {g["name"]: g for g in config.schema()}
        self.assertIn("group_plugins", groups, "应该有单独的「插件」组")
        sections = {s["name"]: s for s in groups["group_plugins"]["sections"]}
        self.assertIn("mypref", sections)
        field = sections["mypref"]["keys"][0]
        self.assertEqual(field["key"], "words")
        self.assertIn("关键词", field["hint"])       # 第一行是标签
        # 注册过之后 config.set 才认得这个键
        config.set("mypref", "words", "x")
        self.assertEqual(config.get("mypref", "words"), "x")

    def test_the_bundled_example_plugin_loads_and_has_its_section(self):
        """仓库里带的「屏蔽词」插件要能直接加载，并且有自己的设置段。"""
        registry = plugin_module.Registry()
        plugin_module.load(ROOT, registry)
        bad = [f"{s.name}: {s.error}" for s in registry.status if not s.ok]
        self.assertEqual(bad, [], "自带插件加载失败（界面只显示红叉，很容易漏看）")
        from app.runtime.config import config

        sections = {s["name"] for g in config.schema() for s in g["sections"]}
        self.assertIn("mute", sections)


class TestMutePlugin(Base):
    def test_it_drops_matching_titles_and_keeps_the_rest(self):
        from app.contracts.models import SOURCE_QQ_GROUP, Message, TaskDraft
        from app.runtime.config import config

        plugin_module.load(ROOT, plugin_module.Registry())
        module = sys.modules["ps_plugin_mute_words"]
        config.set("mute", "words", "砍一刀,代写")

        enricher = module.MuteWords()
        message = Message(msg_id="qq:1", source_kind=SOURCE_QQ_GROUP, text="x")
        kept = enricher.process(message, [
            TaskDraft(title="帮我砍一刀"),
            TaskDraft(title="明天交作业"),
            TaskDraft(title="代写论文"),
        ])
        self.assertEqual([d.title for d in kept], ["明天交作业"])
        self.assertEqual(len(message.raw.get("muted") or []), 2)


class TestSwitches(Base):
    def test_a_disabled_plugin_is_listed_but_not_loaded(self):
        """关掉的插件还得列出来 —— 消失了用户会以为被删了。"""
        self.write("off_one", """
            TITLE = "被关掉的那个"
            def register(registry):
                raise AssertionError("停用的插件不该被执行")
        """)
        registry = plugin_module.Registry()
        plugin_module.load(self.root, registry, disabled=["off_one"])
        status = registry.status[0]
        self.assertTrue(status.disabled)
        self.assertEqual(status.title, "被关掉的那个")
        self.assertIn("已停用", status.describe())

    def test_turning_it_off_writes_the_name_into_the_config(self):
        from types import SimpleNamespace
        from app.runtime.config import config
        from app.ui.bridge import Api

        api = Api(None, [SimpleNamespace(name="mute", title="屏蔽词", ok=True,
                                         error="", section="", description="",
                                         disabled=False)], {}, notices=[])
        self.assertTrue(api.set_plugin("mute", False)["ok"])
        self.assertIn("mute", list(config.get("plugins", "disabled", [])))
        self.assertTrue(api.set_plugin("mute", True)["ok"])
        self.assertNotIn("mute", list(config.get("plugins", "disabled", [])))
