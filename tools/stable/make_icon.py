"""`web/public/favicon.svg` と同じ絵の .ico を描く(スタートメニューのショートカット用)。

SVGを読む依存(cairosvg等)を足さないため、角丸矩形2つを Pillow で描き直す。
favicon.svg の形を変えたらここの座標も合わせること(viewBox 32 の座標をそのまま写してある)。

    python tools/stable/make_icon.py <出力先.ico>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

VIEWBOX = 32
SUPERSAMPLE = 1024  # 大きく描いて縮める(角丸と細線のギザつきを消す)
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def draw() -> Image.Image:
    k = SUPERSAMPLE / VIEWBOX
    image = Image.new("RGBA", (SUPERSAMPLE, SUPERSAMPLE), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    # SVGの stroke は輪郭の中心に乗るので、半分ずつ内外へ広げる。
    stroke = 2.8
    x, y, size, radius = 13.9, 3.4, 14.7, 1.8
    pen.rounded_rectangle(
        [(x - stroke / 2) * k, (y - stroke / 2) * k, (x + size + stroke / 2) * k, (y + size + stroke / 2) * k],
        radius=(radius + stroke / 2) * k,
        outline="#36d1ff",
        width=round(stroke * k),
    )
    x, y, size, radius = 2.0, 12.5, 17.5, 2.6
    pen.rounded_rectangle([x * k, y * k, (x + size) * k, (y + size) * k], radius=radius * k, fill="#ffb454")
    return image


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    out = Path(argv[0])
    out.parent.mkdir(parents=True, exist_ok=True)
    draw().resize((256, 256), Image.LANCZOS).save(out, sizes=SIZES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
