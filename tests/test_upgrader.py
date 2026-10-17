"""一键升级包：它必须"认准旧那份"，而且不能把临时目录里的副本当正经安装。

认错的后果比不做更糟：铺到别的文件夹 = 用户拿到一份配置为空的程序，
再启动就变成两份抢同一个端口（就是新用户反馈里那串"端口被占用"）。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_upgrader():
    spec = importlib.util.spec_from_file_location(
        "upgrader_under_test", ROOT / "tools" / "upgrader.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FindTarget(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = load_upgrader()

    def setUp(self):
        self._argv = list(sys.argv)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        sys.argv = self._argv
        self.tmp.cleanup()

    def _install(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "私人秘书.exe").write_bytes(b"x")
        return folder

    def test_the_folder_given_on_the_command_line_wins(self):
        app = self._install(self.root / "软件")
        sys.argv = ["upgrader.exe", str(app)]
        self.assertEqual(self.up.find_target(), app)

    def test_a_folder_without_the_app_is_rejected(self):
        empty = self.root / "空文件夹"
        empty.mkdir()
        sys.argv = ["upgrader.exe", str(empty)]
        self.assertIsNone(self.up.find_target())

    def test_dragging_the_exe_itself_also_works(self):
        app = self._install(self.root / "软件")
        sys.argv = ["upgrader.exe", str(app / "私人秘书.exe")]
        self.assertEqual(self.up.find_target(), app)

    def test_a_temp_folder_is_recognised(self):
        temp = self.root / "temp"
        temp.mkdir()
        downloaded = self.root / "Downloads" / "私人秘书"
        downloaded.mkdir(parents=True)
        exploded = temp / "Rar$EXa12345" / "私人秘书"
        exploded.mkdir(parents=True)
        self._orig_temp = tempfile.gettempdir
        tempfile.gettempdir = lambda: str(temp)
        try:
            self.assertFalse(self.up._is_temp(downloaded))   # 解压出来的正经文件夹
            self.assertTrue(self.up._is_temp(exploded))      # 压缩包里双击跑的那份
        finally:
            tempfile.gettempdir = self._orig_temp
