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
from app.runtime.config import attachments_dir                  # noqa: E402
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


class TestIngestFileExplainsItself(unittest.TestCase):
    """拖进去的文件没读出东西时，必须能**说清为什么**。

    用户原话："我下完了发送一个 PPT，结果什么都没有读取。"
    以前 `ingest` 只回一个数字（0）—— 为什么（旧版格式读不了 / 扫描件没有文字层 /
    内容不像待办所以进了废件箱）全被丢掉，用户只能猜或者来问作者。
    """

    def setUp(self):
        import run as run_module

        self.run = run_module
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = self.tmp.name
        self.store, self.source, self.pipeline, _sink, _reg = self.run.build(
            Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        if self._old_home is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old_home
        self.tmp.cleanup()

    def write(self, name: str, payload: bytes) -> Path:
        target = Path(self.tmp.name) / name
        target.write_bytes(payload)
        return target

    def test_old_ppt_says_why_it_could_not_be_read(self):
        target = self.write("旧课件.ppt", b"\xd0\xcf\x11\xe0" + b"0" * 64)

        result = self.run.ingest_file(self.store, self.source, self.pipeline, str(target))

        self.assertEqual(result["added"], 0)
        self.assertTrue(any("旧版格式" in note for note in result["notes"]), result)

    def test_a_real_todo_still_gets_created(self):
        target = self.write("作业.txt", "明天下午三点交作业".encode("utf-8"))

        result = self.run.ingest_file(self.store, self.source, self.pipeline, str(target))

        self.assertEqual(result["added"], 1)


class TestJunkBoxCanBeRecovered(unittest.TestCase):
    """废件箱里"被规则拦下的原文"要能捡回来。

    用户原话："他放了两条进废件箱，这不是待办……你看能不能找回来，
    看一下它是什么原因读取的。" 以前那一段只有一句"复制下来贴到
    「＋ 新建待办」里"：两步手活；而带附件的那种更绝 ——
    正文只有 "[图片]"，**什么都复制不出来**。现在一条按钮捡回来。
    """

    def setUp(self):
        import importlib

        import run as run_module

        from app.runtime.config import Config

        # 注意用 import_module：`app.runtime.config` 这个名字在包上被那个
        # 配置**实例**占了，写成 `import app.runtime.config as x` 拿到的是实例。
        config_module = importlib.import_module("app.runtime.config")
        self.run = run_module
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = self.tmp.name
        # 附件一律按"太大、跳过"处理：这条测试要的是**捡回来这个动作**，
        # 不该顺带把 OCR 引擎拉起来（慢，而且依赖本机装了什么）。
        self._old_config = config_module.config
        config_module.config = Config({"extract": {"max_attachment_mb": 0}})
        self.store, source, pipeline, _sink, _reg = self.run.build(
            Path(self.tmp.name) / "t.db")
        self.api = Api(self.store, actions={
            "add_text": lambda text: self.run.ingest(self.store, source, pipeline, text),
            "promote_drop": lambda row, paths: self.run.promote_dropped(
                self.store, pipeline, row, paths),
        })

    def tearDown(self):
        import importlib

        importlib.import_module("app.runtime.config").config = self._old_config
        self.store.close()
        if self._old_home is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old_home
        self.tmp.cleanup()

    def drop(self, text: str, msg_id: str = "") -> int:
        """造一条废件箱记录（走真库）。返回它的编号。"""
        self.store.log_dropped_text(text, "碎片：摘掉时间后没内容",
                                    SOURCE_QQ_GROUP, "某个技术群", msg_id)
        return int(self.store.list_dropped(limit=1)[0]["drop_id"])

    def picture(self, name: str) -> Path:
        folder = attachments_dir()
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        return target

    def test_badge_count_equals_what_the_list_shows(self):
        """左边那个数字必须等于清单里实际有几条。

        2026-10-16 用户报："费件箱有107，他只显示7"。查下来：数字（7）只算了
        "你标成误判的待办"，清单却是"误判 + 被规则拦下的原文"合起来的（7 + 100
        = 107）—— 而且两边各被砍到 100 条（他库里原文有 374 条，更早的根本翻不到）。
        两个数对不上，用户只会以为"东西丢了"。
        """
        for index in range(3):
            self.store.log_dropped_text(f"哈哈哈哈这也太离谱了{index}", "闲聊",
                                        "qq_group", "某个群", f"m:{index}")
        self.assertEqual(self.store.count_dropped(), 3)

        listed = self.api.dropped()["items"]
        self.assertEqual(self.api.counts()["junk"], len(listed),
                         "左边数字和清单条数必须一致")

    def test_promote_turns_a_dropped_line_into_a_todo(self):
        drop_id = self.drop("交到 3 号楼下", "qq:1001")

        self.assertEqual(self.api.promote_drop(drop_id), {"ok": True, "added": 1})
        self.assertIn("交到 3 号楼下", [t.title for t in self.store.list_tasks("open")])
        self.assertEqual(self.store.list_dropped(), [])     # 捡回来就不在废件箱留一份

    def test_promote_keeps_the_picture_when_there_is_no_text(self):
        """用户手上那两条就是这种（群里发的表情包，图里没字）。

        理解层对"群里没字的图"是**刻意不收**的 —— 每条表情包都变待办会刷满列表。
        但用户亲手按了「这条其实有用」，那就按附件收下，图片跟着待办走。
        """
        image = self.picture("qq_1002_1_cafe_图.png")
        drop_id = self.drop("[图片]", "qq:1002")

        self.assertEqual(self.api.promote_drop(drop_id), {"ok": True, "added": 1})
        task = self.store.list_tasks("open")[0]
        self.assertEqual([a.name for a in task.attachments], [image.name])
        self.assertEqual(task.title, "来自某个技术群的图片")   # 没正文时的兜底标题
        self.assertEqual(self.store.list_dropped(), [])

    def test_promote_that_finds_nothing_leaves_it_alone(self):
        """没附件、正文又只有占位符 —— 什么都建不出来。

        这时候**不能**把那笔删掉：用户点一下，废件箱里那条就没了，
        看起来就是"程序把它吃了"。
        """
        drop_id = self.drop("[图片]", "qq:1005")

        self.assertEqual(self.api.promote_drop(drop_id), {"ok": True, "added": 0})
        self.assertEqual(len(self.store.list_dropped()), 1)

    def test_forget_only_clears_the_junk_row(self):
        """「删掉它」只清废件箱这一笔：原始消息和附件都留着。"""
        self.store.save_message(Message(
            msg_id="qq:1003", source_kind=SOURCE_QQ_GROUP,
            source_name="某个技术群", sender="某人", text="哈哈哈哈哈"))
        drop_id = self.drop("哈哈哈哈哈", "qq:1003")

        self.assertEqual(self.api.forget_drop(drop_id), {"ok": True, "removed": 1})
        self.assertEqual(self.store.list_dropped(), [])
        self.assertIsNotNone(self.store.get_message("qq:1003"))

    def test_detail_shows_what_it_read_and_the_picture(self):
        """点开废件箱那条要能答"当时到底收到了什么、程序读到了什么"。"""
        image = self.picture("qq_1004_1_beef_图.png")
        self.store.save_message(Message(
            msg_id="qq:1004", source_kind=SOURCE_QQ_GROUP, source_name="某个技术群",
            sender="某同学", sent_at="2026-10-13 14:19:26", text="[图片]",
            raw={"extract_notes": ["图.png：图片里没有识别到文字"]}))
        drop_id = self.drop("[图片]", "qq:1004")

        detail = self.api.dropped_detail(drop_id)

        self.assertTrue(detail["ok"])
        self.assertEqual(detail["sender"], "某同学")
        self.assertIn("没有识别到文字", detail["notes"][0])
        self.assertEqual(detail["files"][0]["name"], image.name)
        self.assertTrue(detail["preview"].startswith("data:image/png;base64,"))

    def test_gone_drop_is_reported_not_crashed(self):
        for answer in (self.api.dropped_detail(999), self.api.promote_drop(999)):
            self.assertFalse(answer["ok"])
            self.assertIn("不在废件箱", answer["message"])

    def test_only_qq_message_ids_are_looked_up(self):
        """编号来自库里那条记录，拼进路径前必须校验 —— 不校验就等于
        让一条字符串决定程序去读哪个文件。"""
        for bad in ("../../config.toml", "qq:1*", "qq:1/2", "manual:1", ""):
            self.assertEqual(Api._dropped_files(bad), [], bad)


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

        # added = 建了几条；notes = 为什么（没读出东西时界面要拿它解释给用户听）
        self.assertEqual(result, {"ok": True, "added": 2, "notes": []})
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
