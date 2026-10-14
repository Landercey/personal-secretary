"""PDF 提取：先读文本层，读不到再当扫描件渲染成图片走 OCR。

想改这里的话：**这个文件只管 PDF**（页数上限、扫描件判定阈值 `MIN_TEXT_CHARS`、
渲染倍数都在这儿，默认值在 config.toml 的 [extract]）；格式总地图在 `__init__.py`。

为什么要分两步（而不是一律 OCR）：
  文本型 PDF 直接取字是**秒级且 100% 准确**的；OCR 它反而是
  把清清楚楚的字重新"猜"一遍，既慢又可能猜错。只有真正没有文本层的
  扫描件（拍照生成的 PDF）才需要 OCR。

怎么判断是扫描件：取出文本层，字符数低于 MIN_TEXT_CHARS 就当扫描件。
"""

from __future__ import annotations

from pathlib import Path

from ...runtime.config import config
from . import ocr
from .types import ExtractResult

# 文本层字符数低于这个值，就认为这是扫描件。
# 别设成 0：有些 PDF 每页只有页码（"1"、"2"），也会被当成有文本层。
MIN_TEXT_CHARS = 20


def _max_pages() -> int:
    try:
        return max(1, int(config.get("extract", "max_pages", 20) or 20))
    except (TypeError, ValueError):
        return 20


def _render_scale() -> float:
    try:
        return max(1.0, float(config.get("extract", "render_scale", 2.0) or 2.0))
    except (TypeError, ValueError):
        return 2.0


def _read_text_layer(path: Path) -> tuple[str, str]:
    """读文本层。返回 (文本, 说明)。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "未安装 pypdf（pip install pypdf）"
    try:
        reader = PdfReader(str(path))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception:
                return "", "PDF 已加密，读不了"
        limit = _max_pages()
        pages = reader.pages[:limit]
        chunks = [(page.extract_text() or "").strip() for page in pages]
        text = "\n".join(chunk for chunk in chunks if chunk)
        note = f"pypdf 文本层，{len(pages)} 页"
        if len(reader.pages) > limit:
            note += f"（超出 {limit} 页的部分未处理）"
        return text, note
    except Exception as exc:
        return "", f"pypdf 读取失败：{type(exc).__name__}"


def _render_and_ocr(path: Path) -> ExtractResult:
    """把每页渲染成图片再 OCR（扫描件走这条路）。"""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return ExtractResult(method="pdf_render_ocr",
                             note="未安装 pypdfium2（pip install pypdfium2）")
    if not ocr.available():
        return ExtractResult(method="pdf_render_ocr", note="没有可用的 OCR 引擎")

    document = None
    try:
        document = pdfium.PdfDocument(str(path))
        limit = _max_pages()
        page_count = min(len(document), limit)
        chunks: list[str] = []
        for index in range(page_count):
            bitmap = document[index].render(scale=_render_scale())
            result = ocr.recognize_pil(bitmap.to_pil())
            if result.ok:
                chunks.append(f"【第{index + 1}页】{result.text}")
        text = "\n".join(chunks)
        if not text.strip():
            return ExtractResult(method="pdf_render_ocr", note="渲染后 OCR 没有识别到文字")
        return ExtractResult(
            text=text, method="pdf_render_ocr", ok=True,
            note=f"扫描件模式：渲染 {page_count} 页后 OCR",
        )
    except Exception as exc:
        return ExtractResult(method="pdf_render_ocr", note=f"渲染失败：{type(exc).__name__}")
    finally:
        if document is not None:
            try:
                document.close()
            except Exception:
                pass


def extract(path: Path) -> ExtractResult:
    """读一个 PDF：先试文本层，不行再渲染 OCR。"""
    text, note = _read_text_layer(path)
    if len(text.strip()) >= MIN_TEXT_CHARS:
        return ExtractResult(text=text, method="pdf_text", ok=True, note=note)

    ocr_result = _render_and_ocr(path)
    if ocr_result.ok:
        return ocr_result

    # 两条路都不行：把两个原因都带上，用户才知道该装什么
    combined = "；".join(item for item in (note, ocr_result.note) if item)
    return ExtractResult(method="pdf", note=combined or "PDF 里没提取到文字")
