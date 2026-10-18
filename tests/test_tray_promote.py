"""让托盘图标默认露出来（Win11 会把新图标塞进「隐藏的图标」）。

用 HKCU 下一个临时键模拟，不碰真正的通知区域设置。
"""

from __future__ import annotations

import sys
import unittest
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.runtime import desktop   # noqa: E402

BASE = r"Software\personal-secretary-test\NotifyIconSettings"


class TestPromote(unittest.TestCase):
    def setUp(self):
        self.addCleanup(self._cleanup)
        self._cleanup()

    @staticmethod
    def _cleanup() -> None:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, BASE + r"\item1")
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, BASE)
        except OSError:
            pass

    def _make_entry(self, path: str) -> None:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, BASE + r"\item1") as key:
            winreg.SetValueEx(key, "ExecutablePath", 0, winreg.REG_SZ, path)

    def _is_promoted(self) -> int:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, BASE + r"\item1") as key:
            return int(winreg.QueryValueEx(key, "IsPromoted")[0])

    def test_it_marks_our_entry_as_always_visible(self):
        self._make_entry(r"C:\somewhere\私人秘书.exe")
        self.assertTrue(desktop.promote_tray_icon(
            Path(r"C:\somewhere\私人秘书.exe"), base=BASE))
        self.assertEqual(self._is_promoted(), 1)

    def test_it_leaves_other_programs_alone(self):
        self._make_entry(r"C:\other\别的东西.exe")
        self.assertFalse(desktop.promote_tray_icon(
            Path(r"C:\somewhere\私人秘书.exe"), base=BASE))
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, BASE + r"\item1") as key:
            with self.assertRaises(OSError):
                winreg.QueryValueEx(key, "IsPromoted")

    def test_a_missing_key_is_not_an_error(self):
        """Win10 没有这个键：图标本来就在外面，直接返回 False。"""
        self.assertFalse(desktop.promote_tray_icon(
            Path(r"C:\x\私人秘书.exe"), base=r"Software\personal-secretary-test\nope"))
