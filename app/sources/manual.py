"""手动来源：界面粘贴文字、拖入文件。

这是最可靠的入口（不依赖 QQ、不依赖网络），也是 QQ 出问题时唯一的兜底。
所以它不是"临时的"，是一等公民。
"""

from __future__ import annotations

import hashlib
import mimetypes
from datetime import datetime
from pathlib import Path

from ..contracts.interfaces import Source
from ..contracts.models import (
    KIND_DOCUMENT,
    KIND_IMAGE,
    KIND_OTHER,
    SOURCE_MANUAL,
    TIME_FORMAT,
    Attachment,
    Message,
)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff"}
DOC_EXT = {".docx", ".pdf", ".txt", ".md", ".rtf",
           ".xlsx", ".pptx", ".csv", ".doc", ".ppt", ".xls"}


def classify_file(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXT:
        return KIND_IMAGE
    if ext in DOC_EXT:
        return KIND_DOCUMENT
    return KIND_OTHER


def file_sha256(path: str | Path, chunk: int = 1024 * 1024) -> str:
    """文件指纹，内容层去重用。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def build_attachment(path: str | Path) -> Attachment:
    target = Path(path)
    exists = target.is_file()
    return Attachment(
        name=target.name,
        path=str(target),
        mime=mimetypes.guess_type(target.name)[0] or "",
        size=target.stat().st_size if exists else 0,
        kind=classify_file(target),
        sha256=file_sha256(target) if exists else "",
    )


class ManualSource(Source):
    kind = SOURCE_MANUAL
    title = "手动录入"

    def __init__(self, source_name: str = "手动录入") -> None:
        self.source_name = source_name
        self._counter = 0

    def submit(self, message: Message) -> Message:
        """手动录入已经是标准格式，只补上缺失的字段。"""
        return self.normalize(message)

    def normalize(self, message: Message) -> Message:
        message.source_kind = SOURCE_MANUAL
        message.source_name = message.source_name or self.source_name
        message.is_self = True          # 自己敲的，等于自己发的 → 宽判
        if not message.sent_at:
            message.sent_at = datetime.now().strftime(TIME_FORMAT)
        if not message.msg_id:
            message.msg_id = self.next_id()
        return message

    def next_id(self) -> str:
        self._counter += 1
        stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        return f"{SOURCE_MANUAL}:{stamp}-{self._counter}"

    # ------------------------------------------------------------------ 便捷入口
    def from_text(self, text: str, source_name: str | None = None) -> Message:
        """一段文字 → 消息。"""
        message = Message(
            msg_id="",
            source_kind=SOURCE_MANUAL,
            source_name=source_name or self.source_name,
            text=str(text or ""),
        )
        return self.normalize(message)

    def from_files(self, paths: list[str]) -> Message:
        """一组文件 → 一条消息（合规：60 秒合并窗口的等价手动版本）。"""
        message = Message(
            msg_id="",
            source_kind=SOURCE_MANUAL,
            source_name=self.source_name,
            text="",
            attachments=[build_attachment(p) for p in paths],
        )
        return self.normalize(message)
