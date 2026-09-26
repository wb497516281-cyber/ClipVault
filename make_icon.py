"""make_icon.py — 生成 ClipVault 应用图标（托盘用 + exe 用）。

依赖：Pillow。运行：python make_icon.py
产物：
  assets/tray.png    64x64 托盘图标（托盘与运行时直接读它）
  assets/icon.ico    多尺寸 Windows 图标（PyInstaller 打包用）

设计：蓝色圆角方块 + 白色剪贴板轮廓 + 三条文本线，高对比度，小尺寸也清晰。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

#: 资源输出目录
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

#: 主色（与前端 --accent 呼应）
ACCENT = (59, 125, 221)
ACCENT_DARK = (40, 95, 180)
WHITE = (255, 255, 255)
PAPER = (245, 248, 252)


def _clipboard_icon(size: int) -> Image.Image:
    """在 size x size 的画布上绘制剪贴板图标。"""
    scale = 4  # 超采样抗锯齿，最后缩放到目标尺寸
    canvas_size = size * scale
    image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = canvas_size // 16
    radius = canvas_size // 5

    # 圆角方块底
    draw.rounded_rectangle(
        [margin, margin, canvas_size - margin, canvas_size - margin],
        radius=radius,
        fill=ACCENT,
    )
    # 顶部稍深色带，增加层次
    draw.rounded_rectangle(
        [margin, margin, canvas_size - margin, margin + canvas_size // 4],
        radius=radius,
        fill=ACCENT_DARK,
    )
    draw.rectangle(
        [margin, margin + canvas_size // 8, canvas_size - margin, margin + canvas_size // 4],
        fill=ACCENT_DARK,
    )

    # 白色剪贴板
    board = [
        canvas_size * 0.26,
        canvas_size * 0.22,
        canvas_size * 0.74,
        canvas_size * 0.78,
    ]
    draw.rounded_rectangle(board, radius=canvas_size // 24, fill=PAPER)

    # 顶部小夹子
    clip_w = canvas_size * 0.16
    clip_x = (canvas_size - clip_w) / 2
    draw.rounded_rectangle(
        [clip_x, canvas_size * 0.16, clip_x + clip_w, canvas_size * 0.28],
        radius=canvas_size // 40,
        fill=WHITE,
        outline=ACCENT_DARK,
        width=max(2, canvas_size // 90),
    )

    # 三条文本线
    line_color = ACCENT
    line_w = max(2, canvas_size // 80)
    for i, frac in enumerate((0.38, 0.50, 0.62)):
        x0 = canvas_size * 0.34
        x1 = canvas_size * (0.66 if i < 2 else 0.54)  # 第三行短一点
        y = canvas_size * frac
        draw.rounded_rectangle(
            [x0, y - line_w, x1, y + line_w],
            radius=line_w,
            fill=line_color,
        )

    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    """生成 assets/ 下的图标文件。"""
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    tray = _clipboard_icon(64)
    tray.save(ASSETS_DIR / "tray.png")

    # ICO 多尺寸（16/24/32/48/64/128/256），同一个 PIL Image 提供全部尺寸
    icon_image = _clipboard_icon(256)
    icon_image.save(
        ASSETS_DIR / "icon.ico",
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    print(f"已生成：{ASSETS_DIR / 'tray.png'}")
    print(f"已生成：{ASSETS_DIR / 'icon.ico'}")


if __name__ == "__main__":
    main()
