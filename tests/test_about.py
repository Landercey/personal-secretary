"""M2：防背锅的那几件。

重点不是"技术多强"（本地校验永远能被绕过），而是三件事：
  · 买家能在程序里看到"正版免费"和官方地址；
  · 反馈包里能看出这是不是作者发的构建；
  · 名称/图标不随 MIT 一起授权（NOTICE）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ui.bridge import Api                                    # noqa: E402


class TestAbout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._old = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = str(self.home)
        (self.home / "data").mkdir()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old
        self.tmp.cleanup()

    def _api(self) -> Api:
        return Api(store=None, plugins=[], actions={}, notices=[])

    def test_it_says_official_free_and_where_the_data_is(self):
        info = self._api().about()
        self.assertIn("github.com/Landercey/personal-secretary", info["repo"])
        self.assertTrue(info["data"].endswith("data"))
        self.assertTrue(info["version"])

    def test_a_signed_build_is_recognised(self):
        (self.home / "build-info.txt").write_text(
            "v9.9.9｜2026-10-17 18:00｜提交 abc123｜签名 0123456789abcdef",
            encoding="utf-8")
        self.assertTrue(self._api().about()["signed"])

    def test_an_unsigned_build_is_flagged(self):
        (self.home / "build-info.txt").write_text(
            "v9.9.9｜2026-10-17 18:00｜提交 abc123｜未签名（源码/自建）",
            encoding="utf-8")
        self.assertFalse(self._api().about()["signed"])


class TestPaperwork(unittest.TestCase):
    def test_notice_keeps_the_name_clause(self):
        """名称和图标必须单独声明 —— 这是唯一能拦住"换个皮当自己作品卖"的东西。"""
        text = (ROOT / "NOTICE").read_text(encoding="utf-8")
        for anchor in ("私人秘书", "换名换图标", "不随代码一并授权", "免费"):
            self.assertIn(anchor, text)

    def test_readme_has_the_resale_section(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("关于\"倒卖\"和二手渠道", text)
        self.assertIn("NOTICE", text)


class TestSupportEntry(unittest.TestCase):
    """打赏入口：**收到过推送之后**才出现，绝不弹窗。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._old = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = str(self.home)
        (self.home / "data").mkdir()
        from app.store.repository import SqliteStore, open_database

        self.store = SqliteStore(open_database(self.home / "data" / "secretary.db"))

    def tearDown(self):
        self.store.close()
        if self._old is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old
        self.tmp.cleanup()

    def test_it_stays_hidden_until_a_push_has_succeeded(self):
        from app.ui.bridge import Api

        api = Api(self.store, [], {}, notices=[])
        self.assertFalse(api.support()["ready"], "还没推成功过就先别露面")
        self.store.log_push("2026-10-17 07:30", "wecom_bot", True, "ok")
        self.assertTrue(api.support()["ready"])
        self.assertIn("afdian.com", api.support()["url"])

    def test_open_url_only_allows_the_two_known_hosts(self):
        from app.ui.bridge import Api

        api = Api(self.store, [], {}, notices=[])
        # 不真的开浏览器：只验白名单判断
        self.assertFalse(api.open_url("https://example.com/x")["ok"])
        self.assertFalse(api.open_url("file:///C:/Windows")["ok"])
