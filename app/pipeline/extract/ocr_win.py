"""图片识别：调用 Windows 系统自带 OCR。

为什么默认用它：**零安装、零下载、纯 CPU、不联网**。任何 Windows 10/11
自带的，不需要用户装任何东西，一张图约 1 秒。对一个"下载就能跑"的工具，
这是唯一不增加门槛的起点。

它的短板（实测）：手写、黑板照片基本读不出；印刷海报上也会认错个别字
（实测把"下午3:00"读成"下午5：00"，把"误区"读成"俣区"）。
想要更准就装 RapidOCR，见 ocr.py 的选择逻辑。
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from .types import ExtractResult

# 用绝对路径找 PowerShell：不依赖 PATH，也不受"用户在某个目录放了同名 exe"影响。
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
OCR_SCRIPT = Path(__file__).resolve().parent / "win_ocr.ps1"

# 汉字、全角标点的范围
CJK = r"\u3000-\u303f\u4e00-\u9fff\uff00-\uffef"
SP = r"[ \t\u00a0]"

# Windows OCR 把偏旁拆开认的常见错误。左边是被认错的，右边是对的。
# 这些是实测捞出来的，不是猜的 —— 加新条目请附一张能复现的图。
FIXES = {
    "亻乍": "作",
    "亻故": "做",
    "氵主": "注",
    "氵去": "法",
    "扌是": "提",
    "讠井": "讲",
    "汪意": "注意",
    "氵余": "涂",
}


def available() -> bool:
    """本机能不能用系统 OCR。"""
    return POWERSHELL.exists() and OCR_SCRIPT.exists()


def clean(text: str) -> str:
    """整理 OCR 原始输出。

    Windows OCR 会在每个汉字之间插空格，还会把标点认成半角。
    这里只处理**空格和标点**，**绝不能动换行** ——
    动了换行，多行内容会被并成一行，后面按行切分的逻辑全废。
    """
    out_lines: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 汉字之间、汉字与数字之间的空格都去掉（"作 业 3 月" → "作业3月"）
        line = re.sub(rf"(?<=[{CJK}]){SP}+(?=[{CJK}])", "", line)
        line = re.sub(rf"(?<=[{CJK}]){SP}+(?=\d)", "", line)
        line = re.sub(rf"(?<=\d){SP}+(?=[{CJK}])", "", line)
        line = re.sub(rf"(?<=\d){SP}+(?=\d)", "", line)
        # 标点两侧的空格去掉
        line = re.sub(rf"{SP}*([：:，,。；;]){SP}*", r"\1", line)
        line = re.sub(r"[．·]", ".", line)
        # "3 一 11月" 这种把连字符认成"一"的
        line = re.sub(r"(?<=\d)[ \t]*一[ \t]*(?=\d)", "-", line)
        for wrong, right in FIXES.items():
            line = line.replace(wrong, right)
        # 行首 "3，xxx" 统一还原成 "3. xxx"
        line = re.sub(r"^(\d{1,2})[，,]\s*", r"\1. ", line)
        out_lines.append(line.strip())
    return "\n".join(out_lines)


def recognize(image_path: str | Path, timeout: float = 180.0) -> ExtractResult:
    """识别一张图片，返回清理后的文本。"""
    path = Path(image_path)
    if not available():
        return ExtractResult(method="win_ocr", note="系统 OCR 不可用（缺 PowerShell 或脚本）")
    if not path.is_file():
        return ExtractResult(method="win_ocr", note=f"图片不存在：{path.name}")

    with tempfile.TemporaryDirectory(prefix="secretary_ocr_") as tmp:
        out_json = Path(tmp) / "ocr.json"
        try:
            proc = subprocess.run(
                [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(OCR_SCRIPT),
                 "-ImagePath", str(path), "-OutJson", str(out_json)],
                capture_output=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return ExtractResult(method="win_ocr", note=f"识别超时（超过 {int(timeout)} 秒）")
        except Exception as exc:
            return ExtractResult(method="win_ocr", note=f"调用失败：{type(exc).__name__}")

        if not out_json.exists():
            detail = ((proc.stderr or "") + (proc.stdout or "")).strip()[:160]
            return ExtractResult(method="win_ocr", note=f"OCR 未产出结果：{detail}")
        try:
            payload = json.loads(out_json.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            return ExtractResult(method="win_ocr", note=f"OCR 结果读不出来：{type(exc).__name__}")

    if not payload.get("ok"):
        error = str(payload.get("error", ""))[:120]
        return ExtractResult(method="win_ocr", note=f"OCR 引擎报错：{error}")

    text = clean(payload.get("text", ""))
    if not text.strip():
        return ExtractResult(method="win_ocr", note="图片里没有识别到文字")
    return ExtractResult(
        text=text,
        method="win_ocr",
        ok=True,
        note=f"系统 OCR {payload.get('elapsed_ms', 0)} 毫秒，{payload.get('line_count', 0)} 行",
    )


def recognize_pil(image, timeout: float = 180.0) -> ExtractResult:
    """给 PDF 扫描件用：把内存里的图片临时落盘再识别。

    系统 OCR 只认文件路径，不认内存里的图片对象，所以要先存一次。
    """
    with tempfile.TemporaryDirectory(prefix="secretary_ocr_") as tmp:
        target = Path(tmp) / "page.png"
        image.save(target)
        return recognize(target, timeout=timeout)
