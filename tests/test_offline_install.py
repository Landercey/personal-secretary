"""离线安装：校园网证书不被信任时，别人发来的包要能用。

真事：新用户那台机器上三个地址全是
`CERTIFICATE_VERIFY_FAILED` —— 证书问题，不是被墙。
"""

from __future__ import annotations

import ssl
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.runtime import deploy   # noqa: E402


class TestOfflinePackage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_zip(self, size_kb: int = 200) -> Path:
        target = self.dir / "NapCat.Shell.Windows.OneKey.zip"
        with zipfile.ZipFile(target, "w") as bundle:
            bundle.writestr("NapCatInstaller.exe", b"\0" * (size_kb * 1024))
        return target

    def test_a_real_local_zip_is_used_without_touching_the_network(self):
        self.make_zip()
        original = deploy.download
        deploy.download = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("有离线包还去联网了"))
        self.addCleanup(setattr, deploy, "download", original)
        path = deploy.download_onekey(self.dir)
        self.assertEqual(path.name, "NapCat.Shell.Windows.OneKey.zip")

    def test_a_broken_file_is_not_mistaken_for_a_package(self):
        (self.dir / "NapCat.Shell.Windows.OneKey.zip").write_bytes(b"not a zip")
        self.assertIsNone(deploy.local_onekey(self.dir))

    def test_certificate_errors_get_a_specific_explanation(self):
        """证书错误和"被墙"要分开说，否则用户只会一遍遍换网络。"""
        err = urllib.error.URLError(
            ssl.SSLError("CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate"))
        hint = deploy._offline_hint(err, self.dir)
        self.assertIn("证书", hint)
        self.assertIn("OneKey", hint)
        self.assertIn(str(self.dir), hint)

    def test_other_errors_get_no_extra_text(self):
        self.assertEqual(deploy._offline_hint(TimeoutError("timed out"), self.dir), "")
