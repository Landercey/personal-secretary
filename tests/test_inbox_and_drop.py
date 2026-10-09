"""小号收件箱「全盘接收」 + 界面拖文件进来的暂存。

两部分都是"用户明确表达了意图，机器不该再替他判断"这一类：

  · 收件箱：你专门发给小号的东西，连文件里的字也算 ——
    但别的地方照样严判，否则第 6 步修掉的乱收垃圾会回来。
  · 拖进界面的文件：用户点着「变成待办」按钮了，就别再过滤。

所以这里的测试一半在验"该收的收了"，一半在验"不该松的地方没松"。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import (                             # noqa: E402
    SOURCE_MANUAL, SOURCE_QQ_GROUP, SOURCE_QQ_INBOX, Message,
)
from app.pipeline.chat_filter import ChatNoiseEnricher          # noqa: E402
from app.pipeline.parse_enricher import RuleParseEnricher       # noqa: E402
from app.ui.bridge import Api                                   # noqa: E402


def message(kind: str, text: str = "买牛奶", *, is_self: bool = False, **raw) -> Message:
    msg = Message(msg_id="t:1", source_kind=kind, source_name="测试",
                  sender="", is_self=is_self, text=text)
    msg.raw.update(raw)
    return msg


class TestInboxAcceptsEverything(unittest.TestCase):
    """收件箱 = 你自己发给小号的。全盘接收。"""

    def test_inbox_short_todo_is_not_filtered(self):
        """「买牛奶」既没时间也没行动词，但它就是一条待办。"""
        msg = message(SOURCE_QQ_INBOX, "买牛奶", is_self=True)
        self.assertFalse(ChatNoiseEnricher._should_filter(msg))
        self.assertFalse(RuleParseEnricher._strict(msg))

    def test_inbox_keeps_text_read_from_a_file(self):
        """把小号当收件箱的人，发一份文件过去就是明说"这个我要记下来"。

        再拿"文件里大多是资料"把它挡掉，等于这个功能白做。
        """
        msg = message(SOURCE_QQ_INBOX, "字数不少于 2000 字", is_self=True,
                      text_from_file=True)
        self.assertFalse(ChatNoiseEnricher._should_filter(msg))
        self.assertFalse(RuleParseEnricher._strict(msg))


class TestOtherSourcesStayStrict(unittest.TestCase):
    """别松过头：这几条是第 6 步修掉乱收垃圾时定下的，动了就会退回去。"""

    def test_file_text_from_someone_else_is_still_filtered(self):
        """别人的材料严判 —— 实测《实验报告要求.docx》的"字数不少于 2000 字"、
        讲座海报 OCR 出来的"成都理工大学"，都是这么漏进来的。"""
        msg = message(SOURCE_QQ_GROUP, "字数不少于 2000 字", text_from_file=True)
        self.assertTrue(ChatNoiseEnricher._should_filter(msg))
        self.assertTrue(RuleParseEnricher._strict(msg))

    def test_manual_file_via_command_line_is_still_filtered(self):
        """`run.py --file` 走的也是手动来源（is_self=True），
        但它没有 explicit 标记，所以还得严判 —— 这条最容易改漏。"""
        msg = message(SOURCE_MANUAL, "字数不少于 2000 字", is_self=True,
                      text_from_file=True)
        self.assertTrue(ChatNoiseEnricher._should_filter(msg))
        self.assertTrue(RuleParseEnricher._strict(msg))

    def test_manual_typed_text_is_still_wide(self):
        msg = message(SOURCE_MANUAL, "拿快递", is_self=True)
        self.assertFalse(ChatNoiseEnricher._should_filter(msg))
        self.assertFalse(RuleParseEnricher._strict(msg))

    def test_explicit_mark_bypasses_filtering_anywhere(self):
        """界面上点着「变成待办」的，来源是哪都不过滤。"""
        msg = message(SOURCE_QQ_GROUP, "字数不少于 2000 字",
                      text_from_file=True, explicit=True)
        self.assertFalse(ChatNoiseEnricher._should_filter(msg))
        self.assertFalse(RuleParseEnricher._strict(msg))


class TestDropStaging(unittest.TestCase):
    """界面拖进来的文件：先落暂存，用户选了再干活。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = self.tmp.name
        self.seen: list[str] = []
        self.api = Api(None, actions={
            "ingest_file": lambda path: (self.seen.append(path), 2)[1],
            "read_file_text": lambda path: (self.seen.append(path), "读出来的字")[1],
        })
        self.data = Path(self.tmp.name) / "data"

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old_home
        self.tmp.cleanup()

    def stage(self, payload: bytes = b"hello world",
              name: str = "作业要求.docx") -> dict:
        import base64

        return self.api.stage_drop(name, base64.b64encode(payload).decode("ascii"))

    def test_stage_reports_name_and_size(self):
        staged = self.stage(b"x" * 100, "照片.jpg")
        self.assertEqual(staged["name"], "照片.jpg")
        self.assertEqual(staged["size"], 100)
        self.assertTrue((self.data / "drops" / staged["id"]).is_file())

    def test_ingest_moves_the_file_out_of_the_staging_area(self):
        """暂存区是临时地方，待办不能指着它 —— 清暂存就把附件清了。"""
        staged = self.stage()
        result = self.api.ingest_drop(staged["id"])

        self.assertEqual(result, {"ok": True, "added": 2})
        self.assertEqual(len(self.seen), 1)
        archived = Path(self.seen[0])
        self.assertTrue(archived.is_file())
        self.assertEqual(archived.parent.name, "attachments")
        self.assertFalse((self.data / "drops" / staged["id"]).exists())

    def test_peek_reads_without_ingesting(self):
        staged = self.stage()
        result = self.api.peek_drop(staged["id"])

        self.assertEqual(result["text"], "读出来的字")
        self.assertEqual(result["chars"], 5)
        self.assertTrue((self.data / "drops" / staged["id"]).is_file())  # 没被挪走

    def test_discard_deletes_the_staged_copy(self):
        staged = self.stage()
        self.assertEqual(self.api.discard_drop(staged["id"]), {"ok": True})
        self.assertFalse((self.data / "drops" / staged["id"]).exists())

    def test_filename_cannot_escape_the_folder(self):
        """token 是从界面传回来的，直接拼路径 —— 不校验就能读到别处的文件。"""
        for bad in ("../../config.toml", "..\\..\\config.toml",
                    "/etc/passwd", "a/b", "", "随便写点什么"):
            with self.assertRaises(ValueError, msg=bad):
                self.api._drop_path(bad)

    def test_weird_filename_is_sanitized(self):
        staged = self.stage(name="../../坏 名字?.txt")
        self.assertNotIn("..", staged["name"])
        self.assertNotIn("/", staged["name"])
        self.assertTrue((self.data / "drops" / staged["id"]).is_file())

    def test_empty_file_is_refused(self):
        with self.assertRaises(ValueError):
            self.api.stage_drop("空.txt", "")

    def test_broken_base64_is_refused(self):
        with self.assertRaises(ValueError):
            self.api.stage_drop("坏.txt", "这不是 base64！！！")

    def test_oversize_is_refused_before_writing(self):
        """前端会先拦一道，后端这条是第二道 —— 不能让人拿手写请求把内存吃光。"""
        import base64
        import importlib

        from app.runtime.config import Config

        config_module = importlib.import_module("app.runtime.config")
        original = config_module.config
        config_module.config = Config({"ui": {"drop_max_mb": 1}})
        try:
            blob = base64.b64encode(b"x" * (2 * 1024 * 1024)).decode("ascii")
            with self.assertRaises(ValueError) as caught:
                self.api.stage_drop("大文件.bin", blob)
            self.assertIn("1 MB", str(caught.exception))
            self.assertFalse((self.data / "drops").exists()
                             and any((self.data / "drops").iterdir()))
        finally:
            config_module.config = original


def make_lnk(target: Path, lnk: Path) -> bool:
    """造一个**真的** .lnk（交给 Windows 自己的 WScript.Shell 去写）。

    为什么要真造而不是塞几个字节：我们解析靠的也是 Windows 的实现，
    假文件测不出"链路通不通"。造不出来（非 Windows / 没 PowerShell）就返回 False，
    调用方 skip —— 这个项目只支持 Windows，但测试不该在别处直接炸。
    """
    import base64
    import subprocess

    if os.name != "nt":
        return False
    quote = lambda value: str(value).replace("'", "''")      # noqa: E731
    script = (
        "$ProgressPreference = 'SilentlyContinue'\n"
        "$shell = New-Object -ComObject WScript.Shell\n"
        f"$link = $shell.CreateShortcut('{quote(lnk)}')\n"
        f"$link.TargetPath = '{quote(target)}'\n"
        "$link.Save()\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                       capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return lnk.is_file()


class TestShortcutIsFollowed(unittest.TestCase):
    """用户从桌面拖进来的常常是**快捷方式**，不是文件本身。

    真出过这事：拖了一个"抗美援朝军事理论课-PPT.lnk"（1 KB 的指路牌），
    程序说"没读出文字" —— 它当然没字。Windows 双击会跟过去，我们也得跟过去。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self._old_home = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = str(self.base / "home")
        self.real = self.base / "讲座 PPT.pptx"
        self.real.write_bytes(b"PK\x03\x04 fake pptx")
        self.lnk = self.base / "讲座 PPT.lnk"
        if not make_lnk(self.real, self.lnk):
            self.tmp.cleanup()
            self.skipTest("造不出 .lnk（这台机器上没有 PowerShell？）")

        import base64

        self.seen: list[str] = []
        self.api = Api(None, actions={
            "ingest_file": lambda path: (self.seen.append(path), 1)[1],
            "read_file_text": lambda path: (self.seen.append(path), {
                "ok": True, "text": "幻灯片里的字",
                "note": "", "method": "stdlib.pptx"})[1],
        })
        self.staged = self.api.stage_drop(
            self.lnk.name, base64.b64encode(self.lnk.read_bytes()).decode("ascii"))

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old_home
        self.tmp.cleanup()

    def test_resolve_finds_the_real_file(self):
        from app.runtime.desktop import resolve_shortcut

        self.assertEqual(resolve_shortcut(self.lnk), self.real)

    def test_peek_reads_the_target_not_the_pointer(self):
        result = self.api.peek_drop(self.staged["id"])

        self.assertEqual(result["text"], "幻灯片里的字")
        self.assertIn("快捷方式", result["source"])          # 得告诉用户读了哪个
        self.assertEqual(self.seen[-1], str(self.real))       # 读的是真文件

    def test_ingest_copies_the_target_and_keeps_the_original(self):
        self.api.ingest_drop(self.staged["id"])
        archived = Path(self.seen[-1])

        self.assertTrue(archived.is_file())
        self.assertEqual(archived.parent.name, "attachments")
        self.assertEqual(archived.suffix, ".pptx")            # 附件是 PPT，不是 .lnk
        self.assertTrue(self.real.is_file())                  # 用户的原件一动不动

    def test_normal_file_is_not_touched(self):
        """只有 .lnk 才跟；普通文件还是原来那条路（挪暂存件）。"""
        import base64

        staged = self.api.stage_drop(
            "笔记.txt", base64.b64encode("买牛奶".encode()).decode("ascii"))
        self.api.ingest_drop(staged["id"])
        self.assertTrue(Path(self.seen[-1]).name.endswith("笔记.txt"))


if __name__ == "__main__":
    unittest.main()
