"""生成应用图标（assets/app.ico）。

两种来源：
  1. `assets/logo.png` 存在 → 用它（把白底抠成透明，生成多尺寸图标）
  2. 不存在 → 代码画一只鲸鱼兜底

跑法：
    python tools/make_icon.py                 # 用 assets/logo.png，没有就画兜底
    python tools/make_icon.py --source 某图.png   # 换个源图

⚠️ **版权**：`assets/logo.png` 目前用的是网上找的同人作品（原作者抖音号
67538921249，水印已裁掉）。自己电脑上用没问题；
**正式开源发布前必须换成自制或已授权的图**，否则是侵权。

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
BODY = (250, 250, 252, 255)      # 鲸鱼身体：近白，比纯白柔和一点
EYE = (37, 34, 90, 255)          # 眼睛：深靛蓝，不用纯黑

# 多尺寸一起塞进 .ico，Windows 按需要挑
SIZES = (16, 24, 32, 48, 64, 128, 256)

# 源图里水印占的那一条。裁掉它，不修图 ——
# 那块背景是纯白，裁掉完全不碰人物（人物底部到 y=1131，水印从 y≈1165 开始）。
WATERMARK_TOP = 1160


def cut_watermark(im: Image.Image) -> Image.Image:
    """把底部的账号水印裁掉。

    为什么裁而不是涂：涂要用周围像素填补，白底上的灰字涂完会留一块色差；
    裁掉是最干净的，而且这块本来就没有内容。
    """
    if im.height > WATERMARK_TOP:
        return im.crop((0, 0, im.width, WATERMARK_TOP))
    return im


def drop_white_background(im: Image.Image, light_threshold: int = 235) -> Image.Image:
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


def draw(size: int, background: bool = True) -> Image.Image:
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

    # ---- 鲸鱼 ----
    # 所有坐标按"内容区 36..224 水平、56..200 垂直"摆，视觉重心才不会偏。
    #
    # 身体：胖胖的圆角矩形。比椭圆更像"小肥鱼"，小尺寸下也更容易看清。
    artist.rounded_rectangle(B(36, 96, 180, 178), radius=42 * k, fill=BODY)
    # 尾鳍：身体右侧一个上扬的三角
    artist.polygon(P(170, 110, 220, 82, 220, 160, 170, 138), fill=BODY)
    # 前鳍：肚子下面一小片
    artist.polygon(P(84, 156, 116, 156, 96, 194), fill=BODY)
    # 眼睛：唯一的脸部特征。16px 下只有它还看得见，所以它必须够大够黑
    artist.ellipse(B(66, 118, 90, 142), fill=EYE)

    # 喷水：三颗小水珠。
    # 32px 以下不画 —— 缩下去会三颗糊成一坨，反而像脏点。
    if size >= 32:
        for cx, cy, r in ((88, 74, 8), (110, 58, 6), (132, 72, 5)):
            artist.ellipse(B(cx - r, cy - r, cx + r, cy + r), fill=BODY)

    return image.resize((size, size), Image.LANCZOS)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成应用图标")
    parser.add_argument("--source", help="源图（默认 assets/logo.png）")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    source = Path(args.source) if args.source else root / "assets" / "logo.png"

    target = root / "assets" / "app.ico"
    target.parent.mkdir(parents=True, exist_ok=True)

    if source.is_file():
        print(f"用源图：{source}")
        base = from_image(source)
        frames = [base.resize((size, size), Image.LANCZOS) for size in SIZES]
    else:
        print("没找到源图，用代码画一只鲸鱼兜底")
        frames = [draw(size) for size in SIZES]

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
