"""界面接线自检：JS 里调的东西，后端和 HTML 里到底有没有。

为什么值得测：界面是 HTML+JS，Python 测试碰不到它，改坏了要等用户
打开窗口才发现。而这类改动最常见的错就是**接错线**：

  · JS 里 `call('save_settings')`，可 Api 上没有这个方法
    → 用户点"保存"，界面弹一句看不懂的错，或者干脆没反应
  · JS 里 `$('btnSave')`，可 HTML 里那个 id 写成了别的
    → 按钮点了没反应，控制台才看得到 Cannot read properties of null

这两种错静态就能查出来，不必等用户来报。
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ui.bridge import Api                                 # noqa: E402

PAGE = ROOT / "app" / "ui" / "index.html"


class TestWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")

    def test_every_bridge_call_exists_on_api(self):
        called = set(re.findall(r"call\(\s*'([a-zA-Z_][a-zA-Z0-9_]*)'", self.html))
        called |= set(re.findall(r'call\(\s*"([a-zA-Z_][a-zA-Z0-9_]*)"', self.html))
        self.assertTrue(called, "一个桥调用都没扫到，说明这个检查本身失效了")

        missing = sorted(name for name in called if not hasattr(Api, name))
        self.assertEqual(missing, [], f"界面调了但 Api 上没有：{missing}")

    def test_every_element_id_referenced_exists(self):
        """`$('x')` 引用的 id 必须在页面里出现过（含 JS 字符串里动态生成的）。"""
        defined = set(re.findall(r'id="([^"$]+)"', self.html))
        defined |= set(re.findall(r"id=\\?'([^'$]+)'", self.html))
        referenced = set(re.findall(r"\$\(\s*'([^']+)'\s*\)", self.html))
        referenced |= set(re.findall(r'\$\(\s*"([^"]+)"\s*\)', self.html))
        referenced = {name for name in referenced if "$" not in name}

        self.assertTrue(referenced and defined, "扫描规则失效了，这个检查等于没做")
        missing = sorted(name for name in referenced if name not in defined)
        self.assertEqual(missing, [], f"界面里引用了不存在的元素 id：{missing}")

    def test_settings_panel_covers_every_config_section(self):
        """设置面板是照着后端清单画的，这里确认它没有写死某几段。

        清单结构是 分组 → 段 → 项：分组渲染成标签页，段渲染成可折叠的小节，
        高级项折进段底部的「高级」里，锁住的项要给出解锁入口。
        """
        for anchor in ("schema.groups", "renderSection", "details.adv",
                       "data-unlock", "tabpane", "activateTab"):
            self.assertTrue(anchor in self.html, f"界面里找不到：{anchor}")
        self.assertIn("'settings'", self.html)

    def test_page_has_the_four_entry_points(self):
        """用户点得到的入口：新建 / 同步 / 设置 / 列表四个分组。"""
        for anchor in ('id="btnNew"', 'id="btnSync"', 'id="btnSettings"',
                       'id="riskMask"', 'id="guide"', 'id="dropHint"'):
            self.assertIn(anchor, self.html)

    def test_settings_save_bar_is_at_the_bottom(self):
        """保存按钮必须贴底、不许钉在标题旁边。

        用户报的两条：钉在顶上会和关闭的 ✕ 挤在一起；而且改完下面几段
        还得滚回顶上点它 —— 滚着滚着就忘了保存。所以：
        按钮在 `.savebar` 里（sticky 贴底）、有"还没保存"的提醒、
        叉掉窗口前会问一句。
        """
        for anchor in (".savebar", 'id="btnSave"', 'id="saveHint"',
                       "settingsDirty", "updateSettingsDirty", "window.confirm"):
            self.assertIn(anchor, self.html)
        # 保存按钮不能再出现在设置页的标题行里
        self.assertNotIn('<h2>设置</h2><button', self.html)

    def test_drag_and_drop_is_wired(self):
        """拖文件进窗口 → 弹"怎么处理"的面板。

        WebView2 不给网页真实路径，所以内容是 base64 传过去的，
        拖进来时的提示也只能自己画（不然用户以为没反应）。"""
        for anchor in ("wireDrop", "acceptDrop", "readBase64", "stage_drop",
                       "ingest_drop", "peek_drop", "discard_drop", "dropmask"):
            self.assertTrue(anchor in self.html, f"界面里找不到：{anchor}")

    def test_the_detail_panel_can_copy_a_task_out(self):
        """详情里那个「复制这条」：标题+时间+原话+来源，贴到清单软件里能直接用。"""
        for anchor in ("btnCopyTask", "taskText", "copyText", "execCommand"):
            self.assertTrue(anchor in self.html, f"界面里找不到：{anchor}")

    def test_the_ai_section_has_a_test_button(self):
        """「试一下 AI 连接」：填完地址/模型/密钥得能当场验证，别等长通知。"""
        for anchor in ("data-testai", "data-aitestresult", "call('test_ai')"):
            self.assertTrue(anchor in self.html, f"界面里找不到：{anchor}")
        self.assertTrue(hasattr(Api, "test_ai"))

    def test_the_data_section_can_restore_a_backup(self):
        """每天自动备份要有恢复入口 —— 没入口等于白备。"""
        for anchor in ("data-backupsel", "data-restore", "call('backups')",
                       "call('restore_backup'", "window.confirm"):
            self.assertTrue(anchor in self.html, f"界面里找不到：{anchor}")
        self.assertTrue(hasattr(Api, "backups"))
        self.assertTrue(hasattr(Api, "restore_backup"))

    def test_the_about_panel_says_official_and_free(self):
        """关于页是"给买家看"的：正版免费、官方地址、构建签名。"""
        for anchor in ("data-about", "call('about')", "官方构建", "免费开源"):
            self.assertTrue(anchor in self.html, f"界面里找不到：{anchor}")
        self.assertTrue(hasattr(Api, "about"))

    def test_suggestions_and_sync_notes_have_a_place_to_show(self):
        """「等时机到了才出现」的建议、和同步带回来的提醒，都得有地方显示。

        这两样都只能飘一句 toast 的话等于没说 —— 用户正在看别处，两秒就过去了。
        """
        for anchor in ("state.suggestion", "syncNotes", "guide tip", "guide warn",
                       "wireGuide"):
            self.assertTrue(anchor in self.html, f"界面里找不到：{anchor}")

    def test_risk_confirmation_is_not_skippable_by_default(self):
        """契约红线：首次启动必须确认过。遮罩一开始不能是 on 之外的可关闭状态。"""
        self.assertIn('class="mask" id="riskMask"', self.html)
        self.assertIn("confirm_risk", self.html)


class TestUiEntryPoint(unittest.TestCase):
    """`open_ui` 真跑一遍（把 webview 换成假的）。

    **为什么非要测这个**：它是"双击 exe / 双击快捷方式"唯一走到的地方。
    这里出错的话，界面根本不会出现 —— 用户看到的是一个"Failed to execute
    script"的英文报错框。而且**源码运行和打包版都会死**，改这里时漏改一处
    参数名就够了。真发生过一次：参数从 `plugins` 改成 `registry`，函数体里
    还留着 `Api(store, plugins, ...)` → 别人电脑上双击只看到弹框。
    """

    def setUp(self):
        import run as run_module
        import types

        from app.runtime.plugins import Registry

        self.run = run_module
        self.tmp = tempfile.TemporaryDirectory()
        page_dir = Path(self.tmp.name) / "app" / "ui"
        page_dir.mkdir(parents=True)
        (page_dir / "index.html").write_text("<html></html>", encoding="utf-8")
        self._old_resource = run_module.RESOURCE
        run_module.RESOURCE = Path(self.tmp.name)

        self.calls = {}
        fake = types.ModuleType("webview")
        fake.create_window = lambda title, url, **kw: self.calls.update(
            window=(title, url, kw))
        fake.start = lambda **kw: self.calls.update(start=kw)
        self._old_webview = sys.modules.get("webview")
        sys.modules["webview"] = fake
        self.registry = Registry()

    def tearDown(self):
        self.run.RESOURCE = self._old_resource
        if self._old_webview is None:
            sys.modules.pop("webview", None)
        else:
            sys.modules["webview"] = self._old_webview
        self.tmp.cleanup()

    def test_window_opens_with_the_api_attached(self):
        # store/source/pipeline/sink 只在动作被调用时才会用到，这里给 None 就够
        code = self.run.open_ui(None, None, None, None, self.registry)

        self.assertEqual(code, 0, "open_ui 没走到正常返回，说明中途炸了")
        title, url, kw = self.calls["window"]
        self.assertEqual(title, "私人秘书")
        self.assertTrue(url.endswith("index.html"), url)
        self.assertIsInstance(kw["js_api"], Api)
        self.assertEqual(kw["js_api"]._plugins, [])      # 插件的加载结果真的递进去了
        self.assertIn("icon", self.calls["start"])

    def test_js_api_keeps_the_window_out_of_public_attributes(self):
        """窗口只能挂在 `_window` 上 —— 挂到公开属性上会把界面卡死。

        2026-10-14 真踩过（用户原话："怎么打开又是未响应"）：pywebview 生成 JS
        代理时会 `dir()` 遍历 js_api、对每个公开属性 `getattr`，顺着 `api.window`
        一路走进 WinForms 的 `BrowserForm` → 无障碍对象；那一步要跨公寓 COM
        往返、又握着 GIL —— 窗口冻住、页面永远停在"正在连接…"。
        （py-spy 抓到的栈停在 pywebview 的 util.py `get_functions`。）
        """
        code = self.run.open_ui(None, None, None, None, self.registry)
        self.assertEqual(code, 0)
        js_api = self.calls["window"][2]["js_api"]

        self.assertTrue(hasattr(js_api, "_window"),
                        "窗口没递进去，quit_app / 一键更新会失灵")
        self.assertFalse(hasattr(js_api, "window"),
                         "窗口又挂到公开属性上了 —— 界面会未响应")

        leaked = [name for name in dir(js_api)
                  if not name.startswith("_")
                  and getattr(type(getattr(js_api, name)), "__module__", "")
                  .startswith("webview")]
        self.assertEqual(leaked, [], f"这些公开属性会把界面带进 .NET 对象：{leaked}")


if __name__ == "__main__":
    unittest.main()
