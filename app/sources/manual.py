"""手动来源：界面粘贴文字、拖入文件。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  改"哪些后缀算图片/文档"   → 文件顶部的 IMAGE_EXT / DOC_EXT
  加一种新的手动录入方式     → 在这个类上加一个 from_xxx() 方法
  改文件指纹怎么算           → `file_sha256()`（大文件嫌慢可以改成算首尾）
  加一个全新的来源（邮箱）   → 别改这个文件，新写一个 sources/email.py

=====================================================================
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
    """手动录入。实现契约里的 Source 接口。

    虽然叫"手动"，但它遵守的规矩和 QQ 来源完全一样 ——
    产出标准的 Message，不改其它任何东西。所以三种来源能共用同一条管线。
    """

    kind = SOURCE_MANUAL
    title = "手动录入"

    def __init__(self, source_name: str = "手动录入") -> None:
        self.source_name = source_name
        self._counter = 0

    def submit(self, message: Message) -> Message:
        """契约要求的方法。手动录入本来就是标准格式，只补缺失字段。"""
        return self.normalize(message)

    def normalize(self, message: Message) -> Message:
        """补齐 source_kind / source_name / sent_at / msg_id 这几样。

        `is_self=True` 是关键：它让理解层用"宽判"（见 pipeline/parse_enricher.py
        的 `_strict()`）。自己敲的"拿快递"必须留下，不能按群消息的标准过滤掉。
        """
        message.source_kind = SOURCE_MANUAL
        message.source_name = message.source_name or self.source_name
        message.is_self = True          # 自己敲的，等于自己发的 → 宽判
        if not message.sent_at:
            message.sent_at = datetime.now().strftime(TIME_FORMAT)
        if not message.msg_id:
            message.msg_id = self.next_id()
        return message

    def next_id(self) -> str:
        """生成唯一编号：时间戳 + 序号，机器算的，保证同一条不会存两遍。"""
        self._counter += 1
        stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        return f"{SOURCE_MANUAL}:{stamp}-{self._counter}"

    # ------------------------------------------------------------------ 便捷入口
    def from_text(self, text: str, source_name: str | None = None) -> Message:
        """一段文字 → 消息。

        界面上的"粘贴识别"就是调它。
        """
        message = Message(
            msg_id="",
            source_kind=SOURCE_MANUAL,
            source_name=source_name or self.source_name,
            text=str(text or ""),
        )
        return self.normalize(message)

    def from_files(self, paths: list[str]) -> Message:
        """一组文件 → **一条**消息。

        注意是"一条"不是"每条一个"：拖进来 3 张黑板照，说的是同一件事。
        这跟群里 60 秒合并窗口是同一个道理（CONTRACT.md 第七节）。
        """
        message = Message(
            msg_id="",
            source_kind=SOURCE_MANUAL,
            source_name=self.source_name,
            text="",
            attachments=[build_attachment(p) for p in paths],
        )
        return self.normalize(message)
