"""图片识别：RapidOCR（可选增强）。

=====================================================================
为什么会有这个文件 —— 三段实测数据
=====================================================================
拿真实 QQ 截图把两个引擎跑了一遍（工具：tools/compare_ocr.py）：

  讲座海报（印刷体）
    系统自带：...下午5：00...常见俣区...      ← 时间认错了
    RapidOCR：...下午3:00...常见误区...      ← 正确

  手写黑板作业照
    系统自带：2苏 / 一砀 / 02                ← 完全乱码
    RapidOCR：1.背Unit2的四.六级单词 完成P363.P374.P38:56
              完成阅读教程Unit1-4的...       ← 能读

结论：RapidOCR 明显更准，而且正好赢在关键场景上。

=====================================================================
那为什么不默认用它
=====================================================================
它要拖进 onnxruntime，实测占用：
    rapidocr_onnxruntime   15.6 MB（自带模型）
    onnxruntime           346.1 MB   ← 大头
    numpy                  31.0 MB
    合计                  约 390 MB

对一个"下载解压双击就能跑、4 GB 内存也行"的工具，390 MB 不是小数目。
所以做成**可选**：装了自动优先用，没装就退回系统自带。代码路径完全一样。

用户升级方式（一行）：
    pip install rapidocr_onnxruntime

=====================================================================
想改这里的话
=====================================================================
  · 换引擎 / 调优先级 → ocr.py
  · 模型文件路径       → RapidOCR 自己管，装在 site-packages 里
  · 识别顺序/阈值      → 本文件 recognize()
"""

from __future__ import annotations

from pathlib import Path

from .types import ExtractResult

# 进程内缓存引擎实例：加载一次约 1 秒，每张图都重新加载就太亏了。
_engine = None
_load_error = ""


def available() -> bool:
    """本机装没装 RapidOCR。只查有没有这个包，不加载引擎。"""
    import importlib.util

    try:
        return importlib.util.find_spec("rapidocr_onnxruntime") is not None
    except (ImportError, ValueError):
        return False


def engine():
    """拿引擎实例（第一次调用时加载）。加载失败返回 None。"""
    global _engine, _load_error
    if _engine is not None:
        return _engine
    if _load_error:
        return None
    try:
        # ⚠️ 这里**故意**用 importlib 动态导入，不写 `from ... import`：
        # 打包时（tools/build_exe.py）PyInstaller 会把看得见的 import 全部拖进包里，
        # 而 rapidocr → onnxruntime → torch → transformers → librosa… 是 5 GB 的
        # 雪球（实测：整个程序从 90 MB 涨到 5.2 GB）。它是"装了才用"的可选件，
        # 打进包里对绝大多数用户没有意义。
        import importlib

        module = importlib.import_module("rapidocr_onnxruntime")
        _engine = module.RapidOCR()          # type: ignore[attr-defined]
        return _engine
    except Exception as exc:
        _load_error = f"{type(exc).__name__}: {exc}"
        return None


def recognize(image_path: str | Path) -> ExtractResult:
    """识别一张图片。失败返回 ok=False，不抛异常。"""
    path = Path(image_path)
    if not path.is_file():
        return ExtractResult(method="rapid_ocr", note=f"图片不存在：{path.name}")
    instance = engine()
    if instance is None:
        return ExtractResult(method="rapid_ocr",
                             note=f"RapidOCR 加载失败：{_load_error or '未安装'}")
    try:
        result, _elapsed = instance(str(path))
    except Exception as exc:
        return ExtractResult(method="rapid_ocr", note=f"识别异常：{type(exc).__name__}")

    if not result:
        return ExtractResult(method="rapid_ocr", note="图片里没有识别到文字")
    # RapidOCR 返回 [[框坐标, 文字, 置信度], ...]，按识别顺序拼起来
    lines = [str(item[1]).strip() for item in result
             if isinstance(item, (list, tuple)) and len(item) >= 2]
    text = "\n".join(line for line in lines if line)
    if not text:
        return ExtractResult(method="rapid_ocr", note="图片里没有识别到文字")
    return ExtractResult(text=text, method="rapid_ocr", ok=True,
                         note=f"RapidOCR，{len(lines)} 行")


def describe() -> str:
    """一句话说明 RapidOCR 的可用状态，给自检显示。"""
    if not available():
        return "RapidOCR：未安装（pip install rapidocr_onnxruntime 可显著提升识别准确率）"
    if engine() is None:
        return f"RapidOCR：装了但加载失败（{_load_error}）"
    return "RapidOCR：已启用"
