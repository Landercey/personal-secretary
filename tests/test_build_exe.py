"""打包时"别把用户的东西弄丢"这件事要能测。

PyInstaller 打包会删掉整个 `dist/私人秘书/`，而用户填好的 config.toml、
待办附件、下好的 NapCat（1.5 GB）全在里面。所以打包前后要挪出去再挪回来。
作者自己踩过一次，真机上重打包 = 昨天装好的环境没了。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import build_exe                                                 # noqa: E402


class TestMissingFilesAreSpokenOutLoud(unittest.TestCase):
    """关键文件被杀软吃掉时，**要弹框说清楚**，不能只是"双击没反应"。

    用户原话："测试时被 Windows 安全中心拉进隔离了。"
    隔离之后双击 exe 什么都不发生，他只会以为软件坏了 —— 而打包版没有控制台，
    `print()` 等于写进空气。所以这里查的是"缺了哪几个"这个纯逻辑。
    """

    def setUp(self):
        import run as run_module

        self.run = run_module
        self.tmp = tempfile.TemporaryDirectory()
        self._old = run_module.RESOURCE
        run_module.RESOURCE = Path(self.tmp.name)

    def tearDown(self):
        self.run.RESOURCE = self._old
        self.tmp.cleanup()

    def test_lists_every_missing_critical_file(self):
        missing = self.run.missing_app_files()

        self.assertEqual(sorted(missing), sorted(self.run.CRITICAL_FILES))

    def test_nothing_missing_when_the_files_are_there(self):
        for name in self.run.CRITICAL_FILES:
            target = Path(self.tmp.name) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")

        self.assertEqual(self.run.missing_app_files(), [])

    def test_the_real_project_has_them_all(self):
        """这份仓库自己得是完整的 —— 不然打包出来的就是残的。"""
        for name in self.run.CRITICAL_FILES:
            self.assertTrue((Path(self.run.__file__).parent / name).is_file(), name)


class TestKeepUserData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dist = Path(self.tmp.name) / "私人秘书"
        self.dist.mkdir(parents=True)
        (self.dist / "config.toml").write_text("我的配置", encoding="utf-8")
        (self.dist / "data").mkdir()
        (self.dist / "data" / "secretary.db").write_bytes("我的待办".encode("utf-8"))
        (self.dist / "napcat").mkdir()
        (self.dist / "napcat" / "QQ.exe").write_bytes("1.5GB 的那份".encode("utf-8"))
        (self.dist / "私人秘书.exe").write_bytes(b"old exe")   # 这个是产物，不该保住

    def tearDown(self):
        self.tmp.cleanup()

    def test_user_stuff_is_stashed_and_comes_back(self):
        hold = build_exe.stash_user_data(self.dist)

        # 挪走之后，用户的东西不在原位了（这样 PyInstaller 才能删干净）
        self.assertFalse((self.dist / "config.toml").exists())
        self.assertFalse((self.dist / "data").exists())
        self.assertTrue((self.dist / "私人秘书.exe").exists())   # 产物留在原地

        build_exe.restore_user_data(hold, self.dist)
        self.assertEqual((self.dist / "config.toml").read_text(encoding="utf-8"), "我的配置")
        self.assertEqual((self.dist / "data" / "secretary.db").read_bytes(),
                         "我的待办".encode("utf-8"))
        self.assertEqual((self.dist / "napcat" / "QQ.exe").read_bytes(),
                         "1.5GB 的那份".encode("utf-8"))
        self.assertFalse(hold.exists(), "临时的 .keep 目录该自己清掉")

    def test_restore_never_overwrites_the_new_build(self):
        """新包里同名的东西优先（比如以后真往 dist 里塞 plugins/ 模板）。"""
        hold = build_exe.stash_user_data(self.dist)
        (self.dist / "plugins").mkdir()
        (self.dist / "plugins" / "刚打出来的.py").write_text("new", encoding="utf-8")

        build_exe.restore_user_data(hold, self.dist)

        self.assertTrue((self.dist / "plugins" / "刚打出来的.py").exists())
        self.assertEqual((self.dist / "config.toml").read_text(encoding="utf-8"), "我的配置")

    def test_nothing_to_stash_is_fine(self):
        self.assertIsNone(build_exe.stash_user_data(Path(self.tmp.name) / "不存在"))
        build_exe.restore_user_data(None, self.dist)      # 不该炸


if __name__ == "__main__":
    unittest.main()
