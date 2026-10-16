"""解析层统一入口：给一个文件路径，返回文字。

想改这里的话：**加一种新格式就是往 `_READERS` 里加一行**（后缀 → 读法），
别的判断（大小上限、超时、失败怎么说）也都在这个文件里；排版细节看同目录其它文件。

想支持一种新格式，只需要在 `_READERS` 里加一行 ——
调用方（理解层）完全不用改。
"""

from __future__ import annotations

import re
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


_TIME_LIKE = re.compile(r"\d{1,2}\s*[:：]\s*\d{2}")
# 新条目的开头：项目符号、编号、"1."、"一、"
_ITEM_START = re.compile(r"^\s*(?:[-*·•@]|\(?\d+[.)、]|[一二三四五六七八九十][、.)])")
# 话**说完了**的收尾标点。逗号/顿号不在里面 —— 那正是"被换行切开"的信号。
_SENTENCE_END = "。！？!?…"


def unfold_lines(text: str) -> str:
    """把 OCR 的"视觉换行"接回句子。

    2026-10-16 用户报："分行也有问题" —— 海报截图读出来是这样：
        `陪你从刚开始的迷茫与焦`
        `虑，走到找到属于自己的方向。`
    "焦/虑"被切成两行，看着就像乱码。这里把明显是同一句的接回去。

    ⚠️ 四条护卫，少一条就会把表格揉烂（排班表、课表正是靠"一行一条"活的），
    或者把正文粘到海报截图的乱码尾巴上：
      1. 上一行没以句号/问号/感叹号收尾（完了就不该再接）；
      2. 这一行不是新条目（项目符号、编号、"一、"、"@"开头）；
      3. 两行里都没出现"8:00"这种时刻 —— 只要有时刻，就按表格处理，一行不动；
      4. 上一行至少 4 个字。截图里"协""入口"这种一两字的碎片是个小标题，
         不是被切开的句子，接上只会把正文粘进乱码里。

    只用在**图片 OCR** 上：聊天记录里的换行是人为的（排班表就是那样发的），
    不能碰。
    """
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if out and out[-1] and line:
            prev = out[-1]
            if (len(prev) >= 4
                    and prev[-1] not in _SENTENCE_END
                    and not _ITEM_START.match(line)
                    and not _TIME_LIKE.search(prev)
                    and not _TIME_LIKE.search(line)):
                out[-1] = prev + line
                continue
        out.append(line)
    return "\n".join(out)


def _unfold_result(result: ExtractResult) -> ExtractResult:
    """只对"图片 OCR"的结果做折行合并（见 unfold_lines 的说明）。"""
    if result.ok and result.text:
        result.text = unfold_lines(result.text)
    return result


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
            return _unfold_result(ocr.recognize(str(target)))

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
