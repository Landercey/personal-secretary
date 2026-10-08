"""解析结果。单独一个文件是为了避免循环导入。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ExtractResult:
    """一次文件提取的结果。

    失败一律用 `ok=False` + `note` 表达，**不抛异常** ——
    调用方（理解层）不需要为每种文件类型写 try/except，
    而用户也能在界面上看到"这份文件为什么没读出字"。
    """

    text: str = ""
    method: str = ""      # 用了哪条路：stdlib.docx / win_ocr / rapid_ocr / pdf_text …
    ok: bool = False
    note: str = ""        # 人话说明，失败时必填
