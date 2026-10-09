"""压缩包 / 思维导图的读取。

这两个格式之所以内置，是因为它们**本质都是 zip**：标准库直接能读，
不装东西、不加体积。测试盯的是两件事：

  1. 能读出来（含嵌套结构、老版格式、包里套着别的格式）
  2. **解压有上限** —— 一个 1 MB 的压缩包能解出 10 GB，
     不设上限就是拿用户的电脑去赌
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.pipeline.extract import archive, extract_text              # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_zip(self, name: str, members: dict[str, bytes]) -> Path:
        target = self.base / name
        with zipfile.ZipFile(target, "w") as archive_file:
            for member, blob in members.items():
                archive_file.writestr(member, blob)
        return target


class TestReadZip(Base):
    def test_reads_text_members_with_their_names(self):
        target = self.make_zip("作业.zip", {
            "要求.txt": "明天下午三点交作业".encode("utf-8"),
            "备注.md": "记得带实验报告".encode("utf-8"),
        })
        result = extract_text(target)

        self.assertTrue(result.ok)
        self.assertIn("【要求.txt】", result.text)
        self.assertIn("明天下午三点交作业", result.text)
        self.assertIn("记得带实验报告", result.text)

    def test_skips_macos_junk_and_system_files(self):
        """Mac 压出来的包总带 __MACOSX 和 .DS_Store，读进去就是一堆乱码。"""
        target = self.make_zip("mac.zip", {
            "__MACOSX/._要求.txt": b"junk",
            ".DS_Store": b"junk",
            "Thumbs.db": b"junk",
            "真的要求.txt": "周五交".encode("utf-8"),
        })
        result = extract_text(target)

        self.assertIn("周五交", result.text)
        self.assertNotIn("junk", result.text)
        self.assertNotIn("__MACOSX", result.text)

    def test_unsupported_members_are_skipped_silently(self):
        """包里躺着 .exe / .ttf 这种，不该冒进来。"""
        target = self.make_zip("混合.zip", {
            "字体.ttf": b"\x00\x01\x02",
            "说明.txt": "开卷考试".encode("utf-8"),
        })
        result = extract_text(target)

        self.assertIn("开卷考试", result.text)
        self.assertNotIn("字体.ttf", result.text)

    def test_nested_archive_is_reported_not_silently_dropped(self):
        inner = self.make_zip("内层.zip", {"a.txt": b"x"})
        target = self.make_zip("外层.zip", {
            "内层.zip": inner.read_bytes(),
            "说明.txt": "外层说明".encode("utf-8"),
        })
        result = extract_text(target)

        self.assertIn("外层说明", result.text)
        self.assertIn("还有一层", result.text)      # 静默跳过用户会以为丢了东西

    def test_member_count_is_capped(self):
        members = {f"{index:03d}.txt": b"line" for index in range(archive.MAX_MEMBERS + 5)}
        target = self.make_zip("很多.zip", members)
        result = extract_text(target)

        self.assertEqual(result.text.count("【"), archive.MAX_MEMBERS)
        self.assertIn("还有文件没看", result.text)

    def test_declared_size_cannot_blow_past_the_cap(self):
        """压缩包自己报的大小可以撒谎，所以是**边解边数**。

        这里把单个成员上限临时调到 10 字节，塞一段 100 字节的内容进去 ——
        必须在**写下去之前**就停住（所以文件是空的），不能整段放行。
        """
        original = archive.MAX_MEMBER_BYTES
        archive.MAX_MEMBER_BYTES = 10
        try:
            target = self.make_zip("大.txt.zip", {"大.txt": b"A" * 100})
            with zipfile.ZipFile(target) as archive_file:
                info = archive_file.infolist()[0]
                with archive_file.open(info) as source:
                    staged = self.base / "out.txt"
                    archive._copy_capped(source, staged)
            self.assertEqual(len(staged.read_bytes()), 0)   # 超过上限就不再写
        finally:
            archive.MAX_MEMBER_BYTES = original

    def test_broken_zip_returns_empty_not_crash(self):
        broken = self.base / "坏.zip"
        broken.write_bytes(b"this is not a zip at all")
        result = extract_text(broken)

        self.assertFalse(result.ok)
        self.assertTrue(result.note)

    def test_zip_containing_a_readable_office_file(self):
        """包里躺着 docx 时，得走已有的 Office 读取器 —— 不重写一遍。"""
        target = self.make_zip("带文档.zip", {
            "说明.txt": "见附件的做法".encode("utf-8"),
            "空.docx": b"PK\x03\x04 broken but has the extension",
        })
        result = extract_text(target)

        self.assertIn("见附件的做法", result.text)   # 坏的 docx 不拖累整个包


class TestReadXmind(Base):
    def test_modern_xmind_content_json(self):
        content = [{
            "title": "论文",
            "rootTopic": {
                "title": "毕业论文",
                "children": {"attached": [
                    {"title": "第一章 绪论", "children": {"attached": [
                        {"title": "研究背景"}]}},
                    {"title": "第二章 方法"},
                ]},
            },
        }]
        target = self.make_zip("导图.xmind", {
            "content.json": json.dumps(content, ensure_ascii=False).encode("utf-8"),
            "metadata.json": b"{}",
        })
        result = extract_text(target)

        self.assertTrue(result.ok)
        self.assertIn("毕业论文", result.text)
        self.assertIn("第一章 绪论", result.text)
        self.assertIn("研究背景", result.text)
        # 缩进不能丢：丢了就退化成一串没有关系的词
        self.assertIn("  研究背景", result.text)
        self.assertLess(result.text.index("第一章"), result.text.index("研究背景"))

    def test_legacy_xmind_content_xml(self):
        xml = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<xmap-content><sheet><topic><title>旧版导图</title>'
               '<children><topic><title>子主题</title></topic></children>'
               '</topic></sheet></xmap-content>')
        target = self.make_zip("旧导图.xmind", {"content.xml": xml.encode("utf-8")})
        result = extract_text(target)

        self.assertIn("旧版导图", result.text)
        self.assertIn("子主题", result.text)

    def test_xmind_that_is_not_a_zip(self):
        broken = self.base / "坏.xmind"
        broken.write_bytes(b"nope")
        self.assertFalse(extract_text(broken).ok)


class TestUnsupportedHints(Base):
    def test_rar_says_what_to_do_instead(self):
        """只说"不支持"，用户只能来问作者。要说清楚那该怎么办。"""
        target = self.base / "材料.rar"
        target.write_bytes(b"Rar!\x1a\x07\x00")
        result = extract_text(target)

        self.assertFalse(result.ok)
        self.assertIn(".zip", result.note)

    def test_video_says_it_needs_a_model(self):
        target = self.base / "录屏.mp4"
        target.write_bytes(b"\x00\x00")
        result = extract_text(target)

        self.assertFalse(result.ok)
        self.assertIn("语音识别", result.note)

    def test_zip_is_routed_to_the_archive_reader(self):
        from app.pipeline.extract import is_supported, job_type_for

        self.assertTrue(is_supported("a.zip"))
        self.assertTrue(is_supported("b.xmind"))
        self.assertEqual(job_type_for("a.zip"), "doc_parse")
        self.assertEqual(job_type_for("b.xmind"), "doc_parse")
        self.assertIsNone(job_type_for("c.rar"))      # 明确不做的，不进队列


if __name__ == "__main__":
    unittest.main()
