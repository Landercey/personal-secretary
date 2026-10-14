"""OCR 选择器：决定一张图交给哪个引擎。

想改这里的话：**换引擎 / 改"优先用哪个"就改这个文件**（两个引擎分别在
`ocr_rapid.py` 和 `ocr_win.py`）；新格式归谁管看同目录 `__init__.py` 的地图。

规则很简单，就两级：

    装了 RapidOCR  → 用它（更准，实测手写也能读）
    没装           → 用 Windows 系统自带（零安装，印刷体够用）

为什么不做成"两个都跑、比一比选好的"：
  慢一倍，而且没有可靠的判据说哪个更好 —— 两个都错的时候你更不知道选谁。

想强制指定引擎：config.toml 的 [extract] ocr_engine
    auto     自动（默认）
    rapid    只用 RapidOCR，没有就报错
    windows  只用系统自带

想加第三个引擎（比如本地视觉大模型）：在这里加一档，别的代码不用动。
"""

from __future__ import annotations

from ...runtime.config import config
from . import ocr_rapid, ocr_win
from .types import ExtractResult


def selected() -> str:
    """当前会实际使用哪个引擎：`rapid` 或 `windows`。"""
    choice = str(config.get("extract", "ocr_engine", "auto") or "auto").lower()
    if choice == "windows":
        return "windows"
    if choice == "rapid":
        return "rapid"
    return "rapid" if ocr_rapid.available() else "windows"


def available() -> bool:
    """有没有任何一个能用的引擎。"""
    return ocr_win.available() or ocr_rapid.available()


def recognize(image_path: str) -> ExtractResult:
    """识别一张图片。

    首选引擎失败时会**自动退回另一个** —— 用户不该因为"装了 RapidOCR
    但它恰好崩了"就完全收不到消息。
    """
    order = ["rapid", "windows"] if selected() == "rapid" else ["windows", "rapid"]
    last = ExtractResult(method="ocr", note="没有可用的 OCR 引擎")
    for name in order:
        if name == "rapid":
            if not ocr_rapid.available():
                continue
            last = ocr_rapid.recognize(image_path)
        else:
            if not ocr_win.available():
                continue
            last = ocr_win.recognize(image_path)
        # 「图片里确实没字」不算失败，不用换引擎再试一遍
        if last.ok or "没有识别到文字" in last.note:
            return last
    return last


def recognize_pil(image) -> ExtractResult:
    """给 PDF 扫描件用：直接对内存里的图片识别。

    RapidOCR 只认文件路径，所以这里统一先落盘再走 recognize()。
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="secretary_pdf_") as tmp:
        target = Path(tmp) / "page.png"
        image.save(target)
        return recognize(str(target))


def describe() -> str:
    """两个引擎各自的现状，给自检显示。"""
    return f"OCR 引擎：{'RapidOCR' if selected() == 'rapid' else 'Windows 自带'}｜" \
           + ocr_rapid.describe()
