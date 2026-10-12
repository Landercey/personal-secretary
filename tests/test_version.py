"""版本号只有一处来源，而且能被读到、能显示出来。

为什么值得测：版本号是"用户报问题时我拿哪份代码去查"的唯一依据。
它要是和界面/补丁包名字对不上，我就会查错代码 —— 这种错很难发现，
因为界面上那个号看着永远是"正常"的。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.version import VERSION, version_text                     # noqa: E402


class TestVersion(unittest.TestCase):
    def test_version_looks_like_a_version(self):
        parts = VERSION.split(".")
        self.assertEqual(len(parts), 3, VERSION)
        self.assertTrue(all(p.isdigit() for p in parts), VERSION)

    def test_changelog_mentions_the_current_version(self):
        """发版必须往 CHANGELOG 顶上写一段 —— 不然用户不知道新在哪。"""
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## v{VERSION}", text)

    def test_text_is_just_the_version_when_there_is_no_build_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(version_text(Path(tmp)), f"v{VERSION}")

    def test_text_carries_the_build_stamp_when_the_script_wrote_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "build-info.txt").write_text(
                "v0.1.0｜2026-10-12 10:30｜提交 abc1234", encoding="utf-8")
            text = version_text(Path(tmp))
        self.assertTrue(text.startswith(f"v{VERSION}（构建 "), text)
        self.assertIn("提交 abc1234", text)

    def test_settings_payload_carries_the_version(self):
        """设置页顶上那行靠它 —— 用户报问题第一句就是"我是哪一版"。"""
        from app.ui.bridge import Api

        self.assertIn(f"v{VERSION}", Api(None).settings()["version"])


if __name__ == "__main__":
    unittest.main()
