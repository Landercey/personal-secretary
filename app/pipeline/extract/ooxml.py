"""用 Python 标准库读取 Office 文件（docx / pptx / xlsx）。

为什么不用 python-docx / python-pptx / openpyxl：
  这三个文件格式本质就是「zip 包 + 一堆 XML」。标准库的 zipfile 和
  ElementTree 完全够用，省掉三个依赖、省掉几十 MB 体积、
  也省掉"装了但版本不对"这类问题。

代价：只能读**文字**，读不了复杂排版、图表、批注。对我们够了 ——
我们要的是"这份作业要求是什么"，不是还原整个文档。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# XML 命名空间。Word / PPT / Excel 各用一套。
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_docx(path: Path) -> str:
    """Word：逐段落取文字。

    一个 `<w:p>`（段落）= 一行。表格里的文字也在 `w:t` 里，会一起被取到。
    """
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    lines: list[str] = []
    for para in root.iter(f"{W}p"):
        text = "".join(node.text or "" for node in para.iter(f"{W}t")).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def read_pptx(path: Path) -> str:
    """PPT：逐段落取文字（一个文本框里的一段话 = 一行）。

    按文件名排序 = 按幻灯片顺序。否则第 10 页会排在第 2 页前面。
    """
    lines: list[str] = []
    with zipfile.ZipFile(path) as zf:
        slides = sorted(n for n in zf.namelist()
                        if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
        for name in slides:
            root = ET.fromstring(zf.read(name))
            for para in root.iter(f"{A}p"):
                text = "".join(node.text or "" for node in para.iter(f"{A}t")).strip()
                if text:
                    lines.append(text)
    return "\n".join(lines)


def _looks_like_header(cells: list[str]) -> bool:
    """表头判断：每格都很短，且不含数字。

    表头（"姓名 学号 班级"）当待办毫无意义，跳过它。
    但"3号楼"这种带数字的不会被误判成表头。
    """
    return all(len(c) <= 6 for c in cells) and not any(
        any(ch.isdigit() for ch in c) for c in cells
    )


def read_xlsx(path: Path) -> str:
    """Excel：一行一条记录，单元格用中文逗号连接，跳过表头行。"""
    lines: list[str] = []
    with zipfile.ZipFile(path) as zf:
        sheets = sorted(n for n in zf.namelist()
                        if n.startswith("xl/worksheets/") and n.endswith(".xml"))
        for name in sheets:
            root = ET.fromstring(zf.read(name))
            rows: list[list[str]] = []
            for row in root.iter(f"{S}row"):
                cells = ["".join(node.text or "" for node in cell.iter(f"{S}t")).strip()
                         for cell in row.iter(f"{S}c")]
                cells = [c for c in cells if c]
                if cells:
                    rows.append(cells)
            if rows and _looks_like_header(rows[0]):
                rows = rows[1:]
            for cells in rows:
                lines.append("，".join(cells))
    return "\n".join(lines)


def read_plain(path: Path) -> str:
    """纯文本：自动尝试常见编码。

    GB18030 一定要试 —— 国内 Windows 上导出的 txt 大量是 GBK，
    只按 UTF-8 读会全是乱码。
    """
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")
