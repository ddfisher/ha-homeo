"""Draw the Homeo brand icon: a house inside two rainbow arrows that chase each other round a
circle (a feedback loop: *home* + *homeostasis*). Each arrow's head passes over the other's
tail, like a knot, on a dark rounded tile.

Writes custom_components/homeo/brand/icon.png (256 px) and icon@2x.png (512 px).
Run: python tools/make_icon.py  (needs Pillow and numpy)
"""

import colorsys
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

SS = 4  # supersampling for smooth edges
S = 512 * SS
C = S / 2
CLEAR = (0, 0, 0, 0)
TILE = (24, 24, 32, 255)

# Proportions, as fractions of the image size
TILE_RADIUS = 0.22  # corner radius of the tile
ART_SCALE = 0.84  # artwork size within the tile
LOOP_RADIUS = 0.36  # radius of the arrows' circle
ARROW_WIDTH = 0.085  # stroke width of the arrows (heads scale with it)
HOUSE_SIZE = 0.14  # half-width of the house body, roughly
GAP = 11  # size of the dark gap where one arrow crosses the other, in px at 512 px


def rainbow() -> Image.Image:
    """A conic gradient: the hue follows the angle around the centre."""
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
    ang = (np.arctan2(xx - C, -(yy - C)) / (2 * np.pi)) % 1.0
    lut = np.array([colorsys.hsv_to_rgb(h / 1024, 0.85, 1.0) for h in range(1024)], np.float32)
    rgb = (lut[(ang * 1023).astype(np.int32)] * 255).astype(np.uint8)
    return Image.fromarray(rgb, "RGB").convert("RGBA")


def new_mask() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    m = Image.new("L", (S, S), 0)
    return m, ImageDraw.Draw(m)


def paint(img: Image.Image, mask: Image.Image, source: Image.Image) -> Image.Image:
    layer = source.copy()
    layer.putalpha(mask.filter(ImageFilter.GaussianBlur(SS * 0.6)))
    img.alpha_composite(layer)
    return img


def arc_points(a0: float, a1: float, n: int = 600) -> list[tuple[float, float]]:
    """Points along the loop; angles grow clockwise on screen, starting from the top."""
    r = S * LOOP_RADIUS
    return [
        (C + r * math.sin(a0 + (a1 - a0) * i / n), C - r * math.cos(a0 + (a1 - a0) * i / n))
        for i in range(n + 1)
    ]


def arrow_piece(a0: float, a1: float, with_head: bool) -> Image.Image:
    m, d = new_mask()
    pts = arc_points(a0, a1)
    w = S * ARROW_WIDTH
    for x, y in pts:
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=255)
    if with_head:
        (x0, y0), (x1, y1) = pts[-6], pts[-1]
        dx, dy = x1 - x0, y1 - y0
        n = math.hypot(dx, dy)
        dx, dy = dx / n, dy / n
        half, length = w * 1.15, w * 1.35
        d.polygon(
            [
                (x1 - dy * half, y1 + dx * half),
                (x1 + dx * length, y1 + dy * length),
                (x1 + dy * half, y1 - dx * half),
            ],
            fill=255,
        )
    return m


def woven_arrows(colors: Image.Image) -> Image.Image:
    """Two arrows, each head passing over the other's tail, separated by a thin gap."""
    img = Image.new("RGBA", (S, S), CLEAR)

    def lay(img: Image.Image, m: Image.Image) -> Image.Image:
        gap = m.filter(ImageFilter.MaxFilter(SS * GAP | 1)).filter(
            ImageFilter.GaussianBlur(SS * 0.6)
        )
        img = Image.composite(Image.new("RGBA", (S, S), CLEAR), img, gap)
        return paint(img, m, colors)

    span = math.radians(205)
    starts = [math.radians(-10), math.radians(170)]
    # bodies first, then each arrow's last stretch and head on top of the other's tail
    for a0 in starts:
        img = lay(img, arrow_piece(a0, a0 + span - math.radians(40), False))
    for a0 in starts:
        img = lay(img, arrow_piece(a0 + span - math.radians(60), a0 + span, True))
    return img


def house() -> Image.Image:
    m, d = new_mask()
    hw = S * HOUSE_SIZE
    cy = C + hw * 0.05
    d.polygon(
        [(C - hw * 1.3, cy - hw * 0.05), (C, cy - hw * 1.25), (C + hw * 1.3, cy - hw * 0.05)],
        fill=255,
    )
    d.rectangle([C - hw * 0.92, cy - hw * 0.12, C + hw * 0.92, cy + hw * 1.05], fill=255)
    d.rectangle([C - hw * 0.24, cy + hw * 0.38, C + hw * 0.24, cy + hw * 1.06], fill=0)  # door
    return m


def main() -> None:
    colors = rainbow()
    art = paint(woven_arrows(colors), house(), colors)

    icon = Image.new("RGBA", (S, S), CLEAR)
    ImageDraw.Draw(icon).rounded_rectangle([0, 0, S - 1, S - 1], radius=S * TILE_RADIUS, fill=TILE)
    small = art.resize((int(S * ART_SCALE),) * 2, Image.LANCZOS)
    icon.alpha_composite(small, (int(S * (1 - ART_SCALE) / 2),) * 2)

    out = Path(__file__).parents[1] / "custom_components" / "homeo" / "brand"
    out.mkdir(parents=True, exist_ok=True)
    icon.resize((512, 512), Image.LANCZOS).save(out / "icon@2x.png")
    icon.resize((256, 256), Image.LANCZOS).save(out / "icon.png")


if __name__ == "__main__":
    main()
