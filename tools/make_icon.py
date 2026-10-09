"""生成应用图标（assets/app.ico）。

为什么要自己画：Windows 快捷方式默认顶着一个 Python 图标，
看起来像"某个脚本"而不是一个应用。用界面的强调色画一个就行。

图标长这样：靛蓝圆角方块 + 白色对勾 —— 跟界面主色一致，
16px 下也能看清（小尺寸下细节全糊，所以只放一个对勾，不放别的）。

改配色/形状就改这里的常量。生成完要重新创建快捷方式才会生效。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

# 界面主色（index.html 里的 --accent）
ACCENT = (79, 70, 229, 255)
WHITE = (255, 255, 255, 255)

# 多尺寸一起塞进 .ico，Windows 按需要挑
SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw(size: int) -> Image.Image:
    """画一张 size×size 的图。

    所有尺寸都先按 256 的坐标画再缩小 —— 直接用小数坐标画小图
    会糊得更厉害。
    """
    scale = size / 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    artist = ImageDraw.Draw(image)

    def box(*points: float) -> list[tuple[float, float]]:
        return [(x * scale, y * scale) for x, y in zip(points[0::2], points[1::2])]

    # 背景：圆角方块。小尺寸下圆角小一点，不然方块看着像圆
    radius = 56 * scale if size >= 48 else 26 * scale
    artist.rounded_rectangle([(6 * scale, 6 * scale), (250 * scale, 250 * scale)],
                             radius=radius, fill=ACCENT)

    # 对勾：三段折线 + 圆头，粗细随尺寸缩放
    width = max(2, int(30 * scale))
    artist.line(box(62, 132, 108, 180, 196, 82), fill=WHITE, width=width, joint="curve")
    return image


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    target = root / "assets" / "app.ico"
    target.parent.mkdir(parents=True, exist_ok=True)
    frames = [draw(size) for size in SIZES]
    frames[0].save(target, format="ICO",
                   sizes=[(s, s) for s in SIZES], append_images=frames[1:])
    print(f"图标已生成：{target}（{', '.join(str(s) for s in SIZES)} px）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
