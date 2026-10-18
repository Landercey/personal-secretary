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

    def test_it_retries_once_after_the_network_changes(self):
        """从校园网切到热点的那一下会报证书错 —— 等三秒再走一轮，不用用户自己重开。"""
        tries = []

        def boom(url, dest, on_progress=None):
            tries.append(url)
            raise urllib.error.URLError(
                ssl.SSLError("CERTIFICATE_VERIFY_FAILED"))

        original_dl, original_sleep = deploy.download, deploy.time.sleep
        deploy.download = boom
        deploy.time.sleep = lambda s: None          # 测试里别真等
        self.addCleanup(setattr, deploy, "download", original_dl)
        self.addCleanup(setattr, deploy.time, "sleep", original_sleep)

        with self.assertRaises(RuntimeError) as caught:
            deploy.download_onekey(self.dir)
        self.assertEqual(len(tries), 2 * (1 + len(deploy.ONEKEY_MIRRORS)),
                         "每个地址 × 两轮")
        self.assertIn("证书", str(caught.exception))


class TestDropPackage(unittest.TestCase):
    """把 OneKey 包**拖进窗口**就能装 —— 不用找文件夹、不用改名。"""

    def setUp(self):
        import os

        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._old = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = str(self.home)

    def tearDown(self):
        import os

        if self._old is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old
        self.tmp.cleanup()

    def api(self):
        from app.ui.bridge import Api

        return Api(None, [], {}, notices=[])

    @staticmethod
    def b64(blob: bytes) -> str:
        import base64

        return base64.b64encode(blob).decode("ascii")

    def zip_bytes(self, with_installer: bool = True) -> bytes:
        import io

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as bundle:
            if with_installer:
                bundle.writestr("NapCatInstaller.exe", b"MZ" + b"\0" * 2048)
            bundle.writestr("readme.txt", b"hi")
        return buf.getvalue()

    def test_a_real_package_lands_in_the_napcat_folder(self):
        res = self.api().install_napcat_zip(
            "NapCat.Shell.Windows.OneKey.zip", self.b64(self.zip_bytes()))
        self.assertTrue(res["ok"], res)
        self.assertTrue(Path(res["path"]).is_file())
        self.assertEqual(Path(res["path"]).name, "NapCat.Shell.Windows.OneKey.zip")

    def test_a_broken_zip_is_refused_and_not_left_behind(self):
        res = self.api().install_napcat_zip("x.zip", self.b64(b"not a zip"))
        self.assertFalse(res["ok"])
        self.assertFalse((self.home / "napcat" / "NapCat.Shell.Windows.OneKey.zip").exists())

    def test_a_zip_without_the_installer_is_refused(self):
        res = self.api().install_napcat_zip(
            "随便.zip", self.b64(self.zip_bytes(with_installer=False)))
        self.assertFalse(res["ok"])
        self.assertIn("NapCatInstaller", res["message"])
