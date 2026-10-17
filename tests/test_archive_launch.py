"""在压缩包里双击 exe 要拦住。

WinRAR / 7-Zip 双击压缩包里的 exe 时，先把整包解到 %TEMP% 再运行 ——
程序于是把配置和待办写在临时目录里，关掉就没了；再解压一份出来，
两份还会抢同一个 18080（新用户反馈里那一串"端口被占用"就是这么来的）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run                                                   # noqa: E402


class RunningFromArchive(unittest.TestCase):
    def setUp(self):
        self._frozen = getattr(sys, "frozen", None)
        self._had_frozen = hasattr(sys, "frozen")
        self._exe = sys.executable
        self._temp = tempfile.gettempdir
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        sys.executable = self._exe
        tempfile.gettempdir = self._temp
        if self._had_frozen:
            sys.frozen = self._frozen
        elif hasattr(sys, "frozen"):
            del sys.frozen
        self.tmp.cleanup()

    def _simulate(self, exe_dir: Path, temp_dir: Path) -> bool:
        sys.frozen = True                       # type: ignore[attr-defined]
        sys.executable = str(Path(exe_dir) / "私人秘书.exe")
        tempfile.gettempdir = lambda: str(temp_dir)
        return run._running_from_archive()

    def test_exe_inside_the_temp_folder_is_an_archive_launch(self):
        temp = Path(self.tmp.name)
        exploded = temp / "Rar$EXa12345" / "私人秘书"
        exploded.mkdir(parents=True)
        self.assertTrue(self._simulate(exploded, temp))

    def test_a_normal_folder_is_fine(self):
        temp = Path(self.tmp.name) / "temp"
        temp.mkdir()
        real = Path(self.tmp.name) / "Downloads" / "私人秘书"
        real.mkdir(parents=True)
        self.assertFalse(self._simulate(real, temp))

    def test_source_runs_are_never_blocked(self):
        self._simulate(Path(self.tmp.name), Path(self.tmp.name))
        del sys.frozen
        self.assertFalse(run._running_from_archive())
