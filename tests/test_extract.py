"""第 5 步门禁（一）：文件解析。

Word / PPT / Excel 的测试文件是**现场造的**（标准库 zipfile + 一段 XML），
所以不依赖任何外部素材，克隆下来就能跑。

图片 OCR 的用例会跳过 —— 它依赖本机装了什么、以及 Windows 版本，
不适合当作必须通过的测试。真实效果靠 `python run.py --ocr <图片>` 手工验。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.pipeline.extract import extract_text, is_supported, job_type_for   # noqa: E402
from app.pipeline.extract import service                                     # noqa: E402
from app.pipeline.extract.ooxml import read_plain                          # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def make_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(
        f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>' for text in paragraphs)
    document = (
        f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", document)


def make_pptx(path: Path, slides: list[list[str]]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for index, paragraphs in enumerate(slides, start=1):
            body = "".join(
                f'<a:p><a:r><a:t>{text}</a:t></a:r></a:p>' for text in paragraphs)
            # p: 这个前缀在真实的 pptx 里是有声明的，造测试数据时也要声明，
            # 否则 ElementTree 会报 "unbound prefix"。
            slide = (f'<?xml version="1.0"?>'
                     f'<p:sld xmlns:p="{P}" xmlns:a="{A}">{body}</p:sld>')
            zf.writestr(f"ppt/slides/slide{index}.xml", slide)


def make_xlsx(path: Path, rows: list[list[str]]) -> None:
    row_xml = ""
    for row in rows:
        cells = "".join(
            f'<s:c><s:is><s:t>{cell}</s:t></s:is></s:c>' for cell in row)
        row_xml += f"<s:row>{cells}</s:row>"
    sheet = f'<?xml version="1.0"?><s:worksheet xmlns:s="{S}">{row_xml}</s:worksheet>'
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", sheet)


class TestOfficeFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_docx(self):
        path = self.dir / "作业.docx"
        make_docx(path, ["实验报告要求", "1. 下周一前提交", "2. 字数不少于 2000"])
        result = extract_text(path)
        self.assertTrue(result.ok, result.note)
        self.assertIn("下周一前提交", result.text)
        self.assertIn("2000", result.text)
        self.assertEqual(result.method, "stdlib.docx")

    def test_pptx_keeps_slide_order(self):
        """幻灯片要按顺序 —— 排不对的话第 10 页会跑到第 2 页前面。"""
        path = self.dir / "课件.pptx"
        make_pptx(path, [["第一页内容"], ["第二页内容"], ["第十页内容"]])
        result = extract_text(path)
        self.assertTrue(result.ok, result.note)
        self.assertLess(result.text.index("第一页"), result.text.index("第二页"))
        self.assertLess(result.text.index("第二页"), result.text.index("第十页"))

    def test_xlsx_skips_header_row(self):
        """表头（"姓名 学号 班级"）当待办毫无意义，要跳过。"""
        path = self.dir / "名单.xlsx"
        make_xlsx(path, [["姓名", "学号", "班级"], ["张三", "2026001", "2班"]])
        result = extract_text(path)
        self.assertTrue(result.ok, result.note)
        self.assertNotIn("姓名，学号", result.text)
        self.assertIn("张三", result.text)

    def test_legacy_office_tells_user_what_to_do(self):
        """旧版 .doc 是二进制格式，读不了 —— 但要告诉用户怎么办，不能只说失败。"""
        path = self.dir / "旧作业.doc"
        path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
        result = extract_text(path)
        self.assertFalse(result.ok)
        self.assertIn("另存为", result.note)

    def test_missing_file_is_reported_not_raised(self):
        result = extract_text(self.dir / "不存在.docx")
        self.assertFalse(result.ok)
        self.assertIn("不存在", result.note)


class TestUnfoldOcrLines(unittest.TestCase):
    """OCR 的"视觉换行"要接回句子。

    2026-10-16 用户报："分行也有问题" —— 海报截图读出来是
    `陪…迷茫与焦` / `虑，走到…`，"焦/虑"被切成两行，看着像乱码。
    但**聊天记录里的换行不能碰**：排班表、课表正是靠"一行一条"活的。
    """

    def test_sentence_split_by_the_layout_is_joined(self):
        text = "陪你从刚开始的迷茫与焦\n虑，走到找到属于自己的方向。"
        self.assertEqual(
            service.unfold_lines(text),
            "陪你从刚开始的迷茫与焦虑，走到找到属于自己的方向。")

    def test_a_finished_sentence_starts_a_new_line(self):
        self.assertEqual(service.unfold_lines("今天下雨。\n记得带伞"),
                         "今天下雨。\n记得带伞")

    def test_schedule_lines_are_left_alone(self):
        """有时刻的行按表格处理，一行都不许合并。"""
        text = "下周值班表\n张三 周一 8:00-12:00\n李四 周二 8:00-12:00"
        self.assertEqual(service.unfold_lines(text), text)

    def test_numbered_items_are_left_alone(self):
        text = "注意：\n1. 带身份证\n2. 带学生证"
        self.assertEqual(service.unfold_lines(text), text)

    def test_a_mention_line_is_not_glued_to_the_poster_noise(self):
        """截图 OCR 的乱码尾巴不许粘到下一段正文上。

        2026-10-16 用户那条（#44）就是：海报截图读出一堆乱码，紧接着一句
        "@全体成员 同学们好！…"。粘起来的话标题会变成一整串乱码。
        """
        text = "丨山开始学习MNIST - YOLO\n@全体成员   同学们好！今天给大家分享一份指南。"
        self.assertIn("\n@全体成员", service.unfold_lines(text))

    def test_a_short_header_is_not_glued_to_its_body(self):
        text = "入口\nAI 社：https://wiki.uestcai.top/zh/"
        self.assertEqual(service.unfold_lines(text), text)

    def test_the_unfold_is_wired_into_image_extraction(self):
        """接线也要验：图片 OCR 的结果会走这一步。"""
        from app.pipeline.extract.types import ExtractResult

        result = service._unfold_result(
            ExtractResult(text="从迷茫与焦\n虑，走到方向。", method="win_ocr", ok=True))
        self.assertEqual(result.text, "从迷茫与焦虑，走到方向。")


class TestPlainText(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_utf8(self):
        path = self.dir / "a.txt"
        path.write_text("明天交作业", encoding="utf-8")
        self.assertTrue(extract_text(path).ok)

    def test_gbk_is_not_garbled(self):
        """国内 Windows 导出的 txt 大量是 GBK，只按 UTF-8 读会全是乱码。"""
        path = self.dir / "b.txt"
        path.write_bytes("明天交作业".encode("gb18030"))
        result = extract_text(path)
        self.assertTrue(result.ok, result.note)
        self.assertEqual(result.text.strip(), "明天交作业")

    def test_survives_binary_garbage(self):
        path = self.dir / "c.txt"
        path.write_bytes(bytes(range(256)) * 4)
        result = extract_text(path)       # 不抛异常就算过
        self.assertIsInstance(result.ok, bool)

    def test_read_plain_prefers_utf8_sig(self):
        path = self.dir / "d.txt"
        path.write_bytes(b"\xef\xbb\xbfhello")
        self.assertEqual(read_plain(path), "hello")


class TestSupportMatrix(unittest.TestCase):
    def test_supported_extensions(self):
        for ext in (".docx", ".pptx", ".xlsx", ".pdf", ".png", ".jpg", ".txt", ".csv",
                    ".zip", ".xmind"):
            with self.subTest(ext=ext):
                self.assertTrue(is_supported(f"x{ext}"))

    def test_unsupported_extensions(self):
        # .zip/.xmind 2026-10-09 起支持了（本质是 zip，标准库直接读）；
        # .rar/.7z 仍然不做 —— 解压算法有版权，要引二进制库。
        for ext in (".mp4", ".rar", ".7z", ".exe", ".apk", ""):
            with self.subTest(ext=ext):
                self.assertFalse(is_supported(f"x{ext}"))

    def test_job_type(self):
        self.assertEqual(job_type_for("a.png"), "ocr")
        self.assertEqual(job_type_for("a.pdf"), "doc_parse")
        self.assertIsNone(job_type_for("a.mp4"))

    def test_extension_matching_is_case_insensitive(self):
        """手机发来的照片经常是大写后缀（IMG_1234.JPG）。"""
        self.assertTrue(is_supported("IMG_1234.JPG"))
        self.assertEqual(job_type_for("A.PNG"), "ocr")


class TestFailureNeverRaises(unittest.TestCase):
    """任何情况下 extract_text 都只返回结果，不抛异常。

    一份文件读不出来是常事（格式不对、文件损坏、被杀软删了）。
    要是抛异常，一条消息就能把整条接收链路带崩。
    """

    def test_corrupt_zip_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "坏文件.docx"
            path.write_bytes(b"this is not a zip file at all")
            result = extract_text(path)
            self.assertFalse(result.ok)
            self.assertTrue(result.note)

    def test_empty_docx_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "空.docx"
            make_docx(path, [])
            result = extract_text(path)
            self.assertFalse(result.ok)
            self.assertIn("没有文字", result.note)

    def test_pdf_without_libraries_or_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "假.pdf"
            path.write_bytes(b"%PDF-1.4\nnot a real pdf")
            result = extract_text(path)     # 不崩就行
            self.assertIsInstance(result.ok, bool)


if __name__ == "__main__":
    unittest.main()
