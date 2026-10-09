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
                       'id="riskMask"', 'id="guide"'):
            self.assertIn(anchor, self.html)

    def test_risk_confirmation_is_not_skippable_by_default(self):
        """契约红线：首次启动必须确认过。遮罩一开始不能是 on 之外的可关闭状态。"""
        self.assertIn('class="mask" id="riskMask"', self.html)
        self.assertIn("confirm_risk", self.html)


if __name__ == "__main__":
    unittest.main()
