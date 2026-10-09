"""压缩包和思维导图：本质都是 zip 套一层壳，标准库就能拆。

=====================================================================
为什么这两个值得内置，别的格式要等插件
=====================================================================
`.zip` 和 `.xmind` **都是 zip 文件**，Python 标准库（zipfile）直接能读 ——
不装任何东西、不增加打包体积、不联网。这种"顺手就做了"的格式没理由拖到以后。

`.rar` / `.7z` 就不行：解压算法有版权，得引第三方库（还要编译好的二进制），
所以只给一句提示让用户自己解压，不做。

视频、旧版 Office（.doc/.ppt/.xls）也不是"顺手"能做的 ——
前者要配语音识别模型，后者要么装 Office 走 COM、要么引大解析库。
那两类留到插件。

=====================================================================
两条红线
=====================================================================
1. **解压必须设上限**。一个 1 MB 的压缩包可以解出 10 GB（"压缩炸弹"），
   不设上限就是拿用户的电脑去赌。所以三重限制：成员个数、单个大小、总量，
   而且**边解边数**，不信压缩包自己报的大小。
2. **只看认得出来的成员**。压缩包里常混着 `__MACOSX/`、`.DS_Store`、图标、
   字体；逐个硬试不但慢，还会往正文里灌垃圾，最后变成一堆莫名其妙的待办。
"""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

# 处理一个压缩包时的三重上限。它们不是"用户偏好"，是安全阀，所以写死在这里。
MAX_MEMBERS = 30                        # 最多看几个成员文件
MAX_MEMBER_BYTES = 8 * 1024 * 1024      # 单个成员超过这个就不看
MAX_TOTAL_BYTES = 40 * 1024 * 1024      # 解出来的总量到这就停

# 压缩包里几乎一定是垃圾的那些东西
JUNK_PREFIXES = ("__macosx/", ".")
JUNK_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}


def _is_junk(name: str) -> bool:
    lowered = name.lower()
    if lowered.startswith(JUNK_PREFIXES) or "/__macosx/" in lowered:
        return True
    return Path(lowered).name in JUNK_NAMES


def _copy_capped(source, target: Path) -> None:
    """边复制边数上限。

    为什么不直接用 `info.file_size`：那个数是**压缩包自己报的**，
    可以撒谎。真按它去读，一个说"我只有 1 KB"的成员能塞满你的硬盘。
    """
    written = 0
    with open(target, "wb") as out:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            written += len(block)
            if written > MAX_MEMBER_BYTES:
                break
            out.write(block)


def read_zip(target: Path) -> str:
    """压缩包 → 里面能读的文件挨个读一遍拼起来。

    嵌套的压缩包不拆（省得递归没底），只在正文里留一行说明 ——
    静默跳过的话，用户看到"少了几份"会来问。
    """
    from .service import extract_text, is_supported

    chunks: list[str] = []
    total = 0
    seen = 0
    try:
        with zipfile.ZipFile(target) as archive:
            for info in sorted(archive.infolist(), key=lambda item: item.filename):
                if info.is_dir() or _is_junk(info.filename):
                    continue
                if seen >= MAX_MEMBERS:
                    chunks.append(f"（还有文件没看：一个压缩包最多看 {MAX_MEMBERS} 个）")
                    break
                if info.file_size > MAX_MEMBER_BYTES:
                    chunks.append(f"（{info.filename} 太大，跳过了）")
                    continue
                total += info.file_size
                if total > MAX_TOTAL_BYTES:
                    chunks.append("（压缩包解出来太大，后面的没看）")
                    break
                suffix = Path(info.filename).suffix.lower()
                if suffix in (".zip", ".xmind"):
                    chunks.append(f"（{info.filename} 里面还有一层，没拆）")
                    continue
                if not is_supported(info.filename):
                    continue
                seen += 1
                text = _read_member(archive, info)
                if text.strip():
                    chunks.append(f"【{info.filename}】\n{text.strip()}")
    except (zipfile.BadZipFile, OSError):
        return ""
    return "\n\n".join(chunks)


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    """把一个成员解到临时文件，再交给解析层。

    为什么不在这里按扩展名分派：解析层已经有 docx/pptx/xlsx/pdf/图片 OCR
    一整套读取器了，复用它们比自己再写一遍靠谱 ——
    压缩包里躺着的通常正好是这些格式。
    """
    from .service import extract_text

    suffix = Path(info.filename).suffix.lower()
    with tempfile.TemporaryDirectory(prefix="ps-zip-") as folder:
        staged = Path(folder) / f"member{suffix}"
        try:
            with archive.open(info) as source:
                _copy_capped(source, staged)
        except (zipfile.BadZipFile, OSError, RuntimeError):
            return ""
        result = extract_text(staged)
        return result.text if result.ok else ""


# --------------------------------------------------------------------- 思维导图
def read_xmind(target: Path) -> str:
    """思维导图 → 主题树。

    .xmind 是个 zip：新版（XMind 2020+）里是 `content.json`，
    老版里是 `content.xml`。两种都认。
    """
    try:
        with zipfile.ZipFile(target) as archive:
            names = set(archive.namelist())
            if "content.json" in names:
                blob = archive.read("content.json")
                return _topics_from_json(json.loads(blob.decode("utf-8", "replace")))
            for name in sorted(names):
                if name.endswith("content.xml") and not _is_junk(name):
                    return _topics_from_xml(archive.read(name))
    except (zipfile.BadZipFile, OSError, ValueError):
        return ""
    return ""


def _topics_from_json(data) -> str:
    lines: list[str] = []
    sheets = data if isinstance(data, list) else [data]
    for sheet in sheets:
        if not isinstance(sheet, dict):
            continue
        root = sheet.get("rootTopic")
        if isinstance(root, dict):
            _walk_topic(root, 0, lines)
        elif sheet.get("title"):
            lines.append(str(sheet["title"]).strip())
    return "\n".join(lines)


def _walk_topic(topic: dict, depth: int, lines: list[str]) -> None:
    """递归收标题。缩进按层数来 —— 缩进本身就是"这是谁的子项"这个信息，
    丢了它，一张思维导图就退化成一串没有关系的词。"""
    title = str(topic.get("title") or "").strip()
    if title:
        lines.append("  " * depth + title)
    children = topic.get("children")
    if not isinstance(children, dict):
        return
    for group in children.values():
        if not isinstance(group, list):
            continue
        for child in group:
            if isinstance(child, dict):
                _walk_topic(child, depth + 1, lines)


def _topics_from_xml(blob: bytes) -> str:
    try:
        root = ElementTree.fromstring(blob)
    except ElementTree.ParseError:
        return ""
    # 带命名空间时 tag 长这样：{http://...}title，所以用 endswith 而不是 ==
    return "\n".join(
        element.text.strip() for element in root.iter()
        if element.tag.endswith("title") and (element.text or "").strip()
    )
