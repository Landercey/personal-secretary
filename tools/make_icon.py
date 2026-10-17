"""生成应用图标（assets/app.ico）。

代码画的：**一只贪吃豆正咬住一只小企鹅的嘴** ——
"群消息被一口吃掉，只留下待办"。

⚠️ 企鹅是腾讯的注册商标。这里画的是**简笔的小企鹅**（不是照抄 QQ 那只），
但形状仍然容易让人想到它。自己用没问题；要公开发布/商用，最好换成
"消息气泡"或纯贪吃豆版本（`--plain` 一键切回没有企鹅的版本）。

跑法：
    python tools/make_icon.py                  # 画贪吃豆（默认）
    python tools/make_icon.py --source 某图.png  # 换成自己的图（要自制/已授权）

生成完要重跑一次 `python run.py --shortcut`，快捷方式的图标才会更新
（Windows 会缓存图标）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

# 界面主色（index.html 里的 --accent）
# 背景用界面主色（index.html 里的 --accent）
ACCENT = (79, 70, 229, 255)
WHITE = (255, 255, 255, 255)
GOLD = (255, 197, 61, 255)       # 贪吃豆：暖黄，深色背景上最扎眼
BODY = (250, 250, 252, 255)      # 备用身体色（旧图沿用）
EYE = (37, 34, 90, 255)          # 眼睛：深靛蓝，不用纯黑
PENGUIN = (38, 40, 46, 255)      # 企鹅身子：近黑（纯黑在深色任务栏上会糊成一块）
BEAK = (243, 146, 55, 255)       # 嘴和脚
SCARF = (222, 60, 60, 255)       # 围巾：一点红，小尺寸下也认得出是"那只企鹅"

# 多尺寸一起塞进 .ico，Windows 按需要挑
SIZES = (16, 24, 32, 48, 64, 128, 256)

# 源图里水印占的那一条。裁掉它，不修图 ——
# 那块背景是纯白，裁掉不碰图案。
# ⚠️ 这个高度是**按某一张源图量出来的**：换源图时先看一眼水印从哪一行开始，
# 数字对不上就把水印带进图标里了。
WATERMARK_TOP = 1160


def cut_watermark(im: Image.Image) -> Image.Image:
    """把底部的账号水印裁掉。

    为什么裁而不是涂：涂要用周围像素填补，白底上的灰字涂完会留一块色差；
    裁掉是最干净的，而且这块本来就没有内容。
    """
    if im.height > WATERMARK_TOP:
        return im.crop((0, 0, im.width, WATERMARK_TOP))
    return im


def drop_white_background(im: Image.Image, light_threshold: int = 235,
                          noise_floor: float = 12.0) -> Image.Image:
    """白底 → 透明，并且**不留白边**。

    =====================================================================
    为什么不能只做"白就透明"
    =====================================================================
    第一版是从四角灌水，灌到的像素 alpha=0、其余 alpha=255（硬边）。
    问题是**边缘那几个像素既不是纯白也不是纯人物色**，它们是抗锯齿
    过渡出来的半白像素。硬边会把它们留成不透明的浅色 ——
    放到深色背景上就是一圈白边（模糊的头发轮廓外面那圈亮线）。

    =====================================================================
    正确的做法：白底是"合成的背景"，要反算掉
    =====================================================================
    观测到的颜色 C 其实是"人物色 F 盖在白底上"的结果：
        C = a·F + (1-a)·255
    反过来：
        a = 1 - min(C)/255        ← 越白越透明
        F = (C - (1-a)·255) / a   ← 把混进去的白减掉

    用这套算，半白的边缘像素会变成"半透明 + 正确的颜色"，
    放到任何底色上都不会有白圈。

    =====================================================================
    还有一条不能忘：只能动**和边框相连**的区域
    =====================================================================
    人物身上本来就有白的地方（围裙、袜子、高光）。一律按"白=透明"处理的话
    这些地方会变成窟窿。所以先用灌水找出"从画布四角能走到"的那一块，
    只对它做上面那套换算。
    """
    import numpy as np

    image = im.convert("RGBA")
    width, height = image.size

    # ① 灌水找背景：灰度高=可能是背景。深色轮廓会把水挡住，
    #    所以人物内部的白色到不了。
    mask = image.convert("L").point(
        lambda value: 0 if value >= light_threshold else 255)
    for seed in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        if mask.getpixel(seed) == 0:
            ImageDraw.floodfill(mask, seed, 128)
    background = np.asarray(mask) == 128

    # ② 对背景区域按"有多白"算 alpha，并反算掉白底
    arr = np.asarray(image).astype(np.float32)
    rgb, alpha = arr[:, :, :3], arr[:, :, 3]
    whiteness = rgb.min(axis=2) / 255.0            # 1=纯白
    new_alpha = np.where(background, (1.0 - whiteness) * 255.0, alpha)
    # JPEG 的白底**不是纯白**：噪点算出来是一层 alpha 3~8 的雾。
    # 不砍掉的话下一步 getbbox() 会以为整张画布都是内容、裁边白做 ——
    # 表现是图标缩成"一整张白图中间一个小小的图案"。
    # 5% 以下一律当没有；真边缘的半透明像素在 50 以上，砍不到。
    new_alpha = np.where(background & (new_alpha < noise_floor), 0.0, new_alpha)

    safe = np.maximum(new_alpha, 1.0)[:, :, None] / 255.0
    matte = (1.0 - safe) * 255.0                   # 混进去的那层白
    fixed = np.where(background[:, :, None],
                     (rgb - matte) / safe,     # 反算
                     rgb)
    fixed = np.clip(fixed, 0, 255)
    fixed[new_alpha <= 0.5] = 0                    # 全透明的像素颜色无意义

    out = np.dstack([fixed, np.clip(new_alpha, 0, 255)]).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def trim_and_pad(im: Image.Image, margin_ratio: float = 0.03) -> Image.Image:
    """裁到内容边界再补一圈等距留白，最后摆成正方形。

    为什么要这步：源图人物周围有多少留白全看原作者，直接缩成图标会偏。
    """
    box = im.getbbox()
    if box:
        im = im.crop(box)
    side = max(im.size)
    margin = int(side * margin_ratio)
    canvas = Image.new("RGBA", (side + margin * 2, side + margin * 2), (0, 0, 0, 0))
    canvas.paste(im, ((side + margin * 2 - im.width) // 2,
                      (side + margin * 2 - im.height) // 2), im)
    return canvas


def from_image(source: Path) -> Image.Image:
    """一张图 → 可以直接缩成图标的方图。"""
    image = Image.open(source)
    return trim_and_pad(drop_white_background(cut_watermark(image)))


def draw_pac(size: int) -> Image.Image:
    """贪吃豆咬消息气泡。全部是几何图形，缩到 16×16 还认得出。

    为什么是"气泡"不是 QQ 那只企鹅：见文件开头。同一个梗，不碰别人商标。
    """
    # 先在大图上画再缩小，圆角和嘴边的弧度才有抗锯齿（PIL 本身不做）
    ss = 4 if size < 96 else 2
    big = size * ss
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    artist = ImageDraw.Draw(image)
    k = big / 256

    def B(x0, y0, x1, y1):
        return [x0 * k, y0 * k, x1 * k, y1 * k]

    def P(*xy):
        return [(x * k, y * k) for x, y in zip(xy[0::2], xy[1::2])]

    radius = (58 if size >= 48 else 28) * k
    artist.rounded_rectangle(B(6, 6, 250, 250), radius=radius, fill=ACCENT)

    # 气泡先画：让它有一截压在嘴巴里 = "正在被咬"
    artist.rounded_rectangle(B(136, 88, 234, 168), radius=18 * k, fill=WHITE)
    artist.polygon(P(150, 160, 130, 192, 178, 166), fill=WHITE)   # 小尾巴
    if size >= 24:                                                 # 三个点，太小会糊成一坨
        for cx in (166, 186, 206):
            artist.ellipse(B(cx - 6, 122, cx + 6, 134), fill=ACCENT)

    # 贪吃豆：整圆挖掉右边一块 64° 的楔形（start/end 是"剩下的那段"）
    artist.pieslice(B(18, 52, 178, 212), start=32, end=328, fill=GOLD)
    # 眼睛：16px 下只剩它还看得清，所以够大、位置偏上
    artist.ellipse(B(76, 92, 100, 116), fill=EYE)
    return image.resize((size, size), Image.LANCZOS)


def draw(size: int, background: bool = True, penguin: bool = True) -> Image.Image:
    """画一张 size×size 的图。

    统一按 256 的坐标画再缩小 —— 直接用小数坐标画小图会糊得更厉害。
    所有坐标都是 256 画布上的绝对值。
    """
    scale = size / 256

    def pts(*xy: float) -> list[tuple[float, float]]:
        return [(x * scale, y * scale) for x, y in zip(xy[0::2], xy[1::2])]

    def box(x0: float, y0: float, x1: float, y1: float) -> list[float]:
        return [x0 * scale, y0 * scale, x1 * scale, y1 * scale]

    # 先在一张 4 倍大的图上画，再缩下来 —— 这样圆角和小圆点才有抗锯齿。
    # PIL 的 draw 不做抗锯齿，直接画 16px 会全是锯齿。
    ss = 4 if size < 96 else 2
    big = size * ss
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    artist = ImageDraw.Draw(image)
    k = big / 256          # 统一到 256 坐标系

    def B(x0, y0, x1, y1):
        return [x0 * k, y0 * k, x1 * k, y1 * k]

    def P(*xy):
        return [(x * k, y * k) for x, y in zip(xy[0::2], xy[1::2])]

    if background:
        # 圆角方块。小尺寸下圆角小一点，不然方块看着像圆
        radius = (58 if size >= 48 else 28) * k
        artist.rounded_rectangle(B(6, 6, 250, 250), radius=radius, fill=ACCENT)

    # ---- 企鹅（先画，贪吃豆后盖上去 = 被咬住）----
    if penguin:
        artist.ellipse(B(150, 62, 228, 202), fill=PENGUIN)
        artist.ellipse(B(163, 104, 215, 192), fill=WHITE)          # 肚子
        artist.ellipse(B(158, 190, 182, 206), fill=BEAK)           # 脚
        artist.ellipse(B(196, 190, 220, 206), fill=BEAK)
        artist.polygon(P(156, 118, 176, 112, 176, 134), fill=BEAK)  # 嘴（伸进贪吃豆嘴里）
        artist.rectangle(B(150, 138, 226, 152), fill=SCARF)         # 围巾
        artist.polygon(P(150, 138, 130, 156, 150, 164), fill=SCARF)  # 围巾尾巴
        if size >= 32:                                              # 小图不画，会糊成一坨
            artist.ellipse(B(178, 84, 194, 100), fill=WHITE)
            artist.ellipse(B(202, 84, 218, 100), fill=WHITE)
            artist.ellipse(B(180, 88, 189, 97), fill=EYE)
            artist.ellipse(B(204, 88, 213, 97), fill=EYE)

    # ---- 贪吃豆：整圆挖掉右边一块 60° 的楔形 ----
    artist.pieslice(B(24, 54, 176, 206), start=30, end=330, fill=GOLD)
    if size >= 24:
        artist.ellipse(B(84, 92, 102, 110), fill=EYE)

    return image.resize((size, size), Image.LANCZOS)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成应用图标")
    parser.add_argument("--source", help="源图（不给就代码画）")
    parser.add_argument("--plain", action="store_true",
                        help="不画企鹅，只留贪吃豆（要公开发布时用这个，避开商标）")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    source = Path(args.source) if args.source else None

    target = root / "assets" / "app.ico"
    target.parent.mkdir(parents=True, exist_ok=True)

    if source is not None and source.is_file():
        print(f"用源图：{source}")
        base = from_image(source)
        frames = [base.resize((size, size), Image.LANCZOS) for size in SIZES]
    else:
        print("代码画：贪吃豆咬企鹅" if not args.plain else "代码画：只有贪吃豆")
        frames = [draw(size, penguin=not args.plain) for size in SIZES]

    # ⚠️ 必须拿**最大**的那张存，让 PIL 自己往下缩。
    # 第一版写成 frames[0].save(...)（frames[0] 是 16px），
    # 结果 .ico 里只有一个 16×16、文件才 997 字节 ——
    # PIL 生不出比源图更大的尺寸，于是桌面图标是个小图被放大，糊的。
    # 这个坑藏了很久，因为预览时一直是拿 PNG 看的。
    biggest = frames[-1]
    biggest.save(target, format="ICO", sizes=[(s, s) for s in SIZES])

    # =====================================================================
    # 再存一份**带内容指纹的文件名**
    # =====================================================================
    # 为什么需要：Windows 把快捷方式图标按**文件路径**缓存。
    # 改了 app.ico 的内容但路径没变，桌面会继续显示老图标 ——
    # 实测 ie4uinit.exe -show 刷不动，得重启资源管理器才行，太打扰。
    # 换个文件名就绕过去了：路径变了 = 缓存必然失效 = 一定读新图。
    import hashlib
    import json

    blob = target.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()[:8]
    stamped = target.with_name(f"app-{digest}.ico")
    stamped.write_bytes(blob)
    for old in target.parent.glob("app-*.ico"):      # 清掉上一版，别堆一堆
        if old != stamped:
            try:
                old.unlink()
            except OSError:
                pass
    (target.parent / "icon.json").write_text(
        json.dumps({"shortcut_icon": stamped.name}, ensure_ascii=False),
        encoding="utf-8")

    saved = sorted(Image.open(target).ico.sizes())
    print(f"图标已生成：{target}")
    print(f"  快捷方式用：{stamped.name}（文件名跟着内容变，绕开 Windows 图标缓存）")
    print(f"  实际包含尺寸：{', '.join(f'{w}×{h}' for w, h in saved)}")
    if len(saved) < len(SIZES):
        print(f"  ⚠️ 期望 {len(SIZES)} 个尺寸，只存进去 {len(saved)} 个")
    print("重跑 python run.py --shortcut 才会更新桌面快捷方式的图标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
