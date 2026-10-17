"""残留的 NapCat / QQ 进程。

真事：启动器把 QQ 拉起来之后自己会退出，于是"停止后台"经常清不掉那一窝 QQ。
这台机器上堆到 23 个启动器 + 92 个 QQ.exe，全都抢同一个号的登录 ——
表现就是"启动很久没反应""扫了码还是登不上"，而且白占好几百 MB 内存。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.runtime import deploy   # noqa: E402


class TestStrays(unittest.TestCase):
    def test_it_can_see_this_process(self):
        """先证明"枚举进程"这条路是通的，否则下面的空结果证明不了任何事。"""
        pids = {pid for pid, _ in deploy._image_paths()}
        self.assertIn(os.getpid(), pids)

    def test_it_never_counts_this_process(self):
        """自己绝不能在被清掉的名单里。"""
        folder = Path(sys.executable).resolve().parent
        self.assertNotIn(os.getpid(), deploy.stray_processes(folder))

    def test_an_unrelated_folder_has_nothing_to_kill(self):
        """按**路径**认，不按进程名杀：别的目录里的 QQ.exe 一个都不能碰。"""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(deploy.stray_processes(Path(tmp)), [])
            self.assertEqual(deploy.stop_all(Path(tmp)), 0)
