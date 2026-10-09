"""解析层统一入口：给一个文件路径，返回文字。

想支持一种新格式，只需要在 `_READERS` 里加一行 ——
调用方（理解层）完全不用改。
"""

from __future__ import annotations

from pathlib import Path

from . import archive, ocr, ooxml, pdf
from .types import ExtractResult

# 各类扩展名。加新格式时这三个集合和 _READERS 一起改。
OFFICE_EXTS = {".docx", ".pptx", ".xlsx"}
LEGACY_OFFICE_EXTS = {".doc", ".ppt", ".xls"}       # 旧版二进制格式，读不了
TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".log", ".json"}
PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v"}
ARCHIVE_EXTS = {".zip"}                 # 只认 zip：rar/7z 的解压算法有版权
MINDMAP_EXTS = {".xmind"}               # 本质也是 zip

# 格式 → 读取函数。加新格式在这一行登记即可。
_READERS = {
    ".docx": ooxml.read_docx,
    ".pptx": ooxml.read_pptx,
    ".xlsx": ooxml.read_xlsx,
    ".zip": archive.read_zip,
    ".xmind": archive.read_xmind,
}

# 明确不做的格式，给一句"那该怎么办"。只说"不支持"，用户只能来问作者。
UNSUPPORTED_HINTS = {
    ".rar": "压缩包只认 .zip —— .rar / .7z 的解压算法有版权，标准库做不了。"
            "先解压出来，或者用 zip 重新压一份",
    ".7z": "同上：只认 .zip，请先解压或用 zip 重新压一份",
    ".tar": "请压成 .zip 再发（.tar/.tar.gz 没做）",
    ".gz": "请压成 .zip 再发（.tar/.tar.gz 没做）",
}


def job_type_for(path: str | Path) -> str | None:
    """这个文件该排什么活；None 表示不支持、不该排队。"""
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "ocr"
    if ext in OFFICE_EXTS or ext in LEGACY_OFFICE_EXTS \
            or ext in TEXT_EXTS or ext in PDF_EXTS \
            or ext in ARCHIVE_EXTS or ext in MINDMAP_EXTS:
        return "doc_parse"
    return None          # 视频、压缩包、可执行文件：按设计不处理


def is_supported(path: str | Path) -> bool:
    return job_type_for(path) is not None


def extract_text(path: str | Path) -> ExtractResult:
    """把文件变成纯文本。任何异常都转成 ok=False 的结果，不抛出去。"""
    target = Path(path)
    if not target.exists():
        return ExtractResult(note=f"文件不存在：{target.name}")

    ext = target.suffix.lower()
    try:
        if ext in _READERS:
            text = _READERS[ext](target)
            if not text.strip():
                return ExtractResult(method=f"stdlib{ext}", note="文件里没有文字内容")
            return ExtractResult(text=text, method=f"stdlib{ext}", ok=True,
                                 note="Python 标准库直接读取")

        if ext in LEGACY_OFFICE_EXTS:
            # 旧版 .doc/.ppt/.xls 是二进制格式，标准库解不开，
            # 也不值得为它引 python-docx 那种大依赖。直接告诉用户怎么办。
            return ExtractResult(
                method="legacy_office",
                note=f"旧版格式（{ext}）读不了，请用 Office 另存为 "
                     f"{ext}x 再发一次",
            )

        if ext in TEXT_EXTS:
            text = ooxml.read_plain(target)
            if not text.strip():
                return ExtractResult(method="plain", note="文本文件是空的")
            return ExtractResult(text=text, method="plain", ok=True, note="纯文本直接读取")

        if ext in PDF_EXTS:
            return pdf.extract(target)

        if ext in IMAGE_EXTS:
            return ocr.recognize(str(target))

        if ext in VIDEO_EXTS:
            return ExtractResult(
                method="skip",
                note="视频要先把声音转成文字（配语音识别模型），那个还没做 —— "
                     "想留个念的话，可以把视频里关键的一帧截图发过来")

        hint = UNSUPPORTED_HINTS.get(ext)
        return ExtractResult(method="unsupported",
                             note=hint or f"暂不支持的文件类型：{ext or '无扩展名'}")
    except Exception as exc:
        return ExtractResult(method="error", note=f"提取异常：{type(exc).__name__}: {exc}")
