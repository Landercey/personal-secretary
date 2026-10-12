"""自动检查更新：比版本号、读缓存、问不到就安静地算了。

为什么要测：这段代码**每次启动都会跑**，写错的表现是"卡住界面"或
"天天弹有新版本"——两种都比没有这个功能更烦人。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.runtime import update                                     # noqa: E402
from app.version import VERSION                                    # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = self.tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old
        self.tmp.cleanup()


class TestVersionCompare(unittest.TestCase):
    def test_parses_tags_with_or_without_v(self):
        self.assertEqual(update.parse_version("v0.2.1"), (0, 2, 1))
        self.assertEqual(update.parse_version("0.2"), (0, 2, 0))
        self.assertEqual(update.parse_version("乱写的"), (0,))

    def test_newer_means_newer(self):
        self.assertTrue(update.is_newer("0.2.0", "0.1.9"))
        self.assertTrue(update.is_newer("v1.0.0", "0.9.9"))
        self.assertFalse(update.is_newer("0.1.0", "0.1.0"))
        self.assertFalse(update.is_newer("0.0.9", "0.1.0"))

    def test_garbage_remote_is_never_newer(self):
        """答非所问（比如被运营商劫持回了一页 HTML）不能当成新版。"""
        self.assertFalse(update.is_newer("", VERSION))
        self.assertFalse(update.is_newer("<html>", "0.1.0"))


class TestCache(Base):
    def test_stale_cache_means_ask_again(self):
        self.assertTrue(update.need_check(24))          # 什么都没记过 → 该问
        update.write_cache("0.1.1")
        self.assertFalse(update.need_check(24))         # 刚问过 → 别烦 GitHub

    def test_zero_hours_always_asks(self):
        update.write_cache("0.1.1")
        self.assertTrue(update.need_check(0))

    def test_old_cache_is_refreshed(self):
        update.write_cache("0.1.1")
        path = update.cache_path()
        data = path.read_text(encoding="utf-8").replace("checked_at", "checked_at")
        import json

        payload = json.loads(data)
        payload["checked_at"] = time.time() - 48 * 3600
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertTrue(update.need_check(24))

    def test_failed_check_is_recorded_as_no_update(self):
        """问不到（断网/仓库私有）要记成本地版本 —— 不然每次启动都去试一遍。"""
        original = update.fetch_latest
        update.fetch_latest = lambda repo, timeout=6.0: ""
        try:
            self.assertEqual(update.check_now("someone/repo"), "")
        finally:
            update.fetch_latest = original
        self.assertFalse(update.need_check(24))
        self.assertFalse(update.summary()["has_new"])

    def test_summary_says_there_is_a_new_version(self):
        update.write_cache("9.9.9")
        data = update.summary()
        self.assertTrue(data["has_new"])
        self.assertEqual(data["latest"], "9.9.9")
        self.assertIn("github.com", data["url"])


class TestNoNetworkOnTheUiThread(Base):
    def test_summary_never_touches_the_network(self):
        """界面每 5 秒拉一次 config_summary —— 那里绝对不能发请求。

        做法就是：summary() 只读缓存文件。这里把"发请求"换成"炸掉"，
        只要它没炸，就说明界面那条路上没有网络。
        """
        def boom(*args, **kwargs):
            raise AssertionError("summary() 不该发网络请求")

        original = update._get
        update._get = boom
        try:
            update.summary()
        finally:
            update._get = original


if __name__ == "__main__":
    unittest.main()
