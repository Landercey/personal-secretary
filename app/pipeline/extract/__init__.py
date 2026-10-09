"""文件解析层：把 Word / PPT / Excel / PDF / 图片 / 压缩包 / 思维导图 变成纯文本。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  支持一种新文件格式        → `service.py` 里的 `_READERS` 加一行
  换 OCR 引擎              → `ocr.py`（现在是"有 RapidOCR 就用它，否则用系统自带"）
  改系统 OCR 的清理规则     → `ocr_win.py` 的 `clean()`
  改 PDF 处理多少页         → config.toml 的 [extract] max_pages
  改扫描件判定阈值          → `pdf.py` 的 MIN_TEXT_CHARS

设计原则（沿用旧项目验证过的结论）：
  1. 能用 Python 标准库解决的绝不引第三方依赖（docx/pptx/xlsx 本质是 zip+XML）；
  2. 图片默认走 **Windows 系统自带 OCR**：零安装、纯 CPU、不联网、约 1 秒一张；
  3. 装了 `rapidocr_onnxruntime` 就优先用它 —— 实测明显更准（尤其手写和海报），
     代价是 onnxruntime 要占约 390 MB，所以做成可选；
  4. 任何一步失败都返回结果对象，不抛异常 —— 一份文件读不出来，
     不能把整条链路带崩。

**什么格式值得内置、什么该等插件**（2026-10-09 定）：

    顺手就做   .zip / .xmind —— 本质都是 zip，标准库直接读，零依赖零体积
    得配模型   视频（要语音识别）、手写体（要视觉模型）→ 插件
    得靠外部   旧版 .doc/.ppt/.xls（要 Office 或大解析库）→ 以后再说
    不做       .rar / .7z（解压算法有版权，要引二进制库）→ 只给提示
=====================================================================
"""

from .service import extract_text, is_supported, job_type_for
from .types import ExtractResult

__all__ = ["extract_text", "is_supported", "job_type_for", "ExtractResult"]
