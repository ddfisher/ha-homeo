"""Draw the Homeo brand icon: two rainbow arrows circling, each chasing the other's tail
(a feedback loop). Writes custom_components/homeo/brand/icon.png and icon@2x.png.

Run: python tools/make_icon.py  (needs Pillow and numpy)
"""

import colorsys
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

SS = 4  # supersampling for smooth edges
SIZE = 512 * SS
C = SIZE / 2
R = SIZE * 0.335  # radius of the loop
W_TAIL, W_HEAD = SIZE * 0.035, SIZE * 0.115  # stroke width tapers from tail to head
HEAD_LEN = math.radians(34)  # arrowhead length, as an angle along the loop
GAP = math.radians(4)  # space between a head and the next tail
ARC = math.pi - GAP - HEAD_LEN  # angle covered by each arrow's shaft


def radius_at(t: float) -> float:
    """Distance from the centre at fraction t (0 = tail, 1 = tip) along an arrow: the tail
    starts inside, the arrow swings out, and the head hooks back in at the other tail."""
    return R * (0.62 + 0.55 * math.sin(math.pi * 0.86 * t))


def point(angle: float, radius: float) -> tuple[float, float]:
    # angles grow clockwise on screen, starting from the top
    return C + radius * math.sin(angle), C - radius * math.cos(angle)


def draw_arrow(d: ImageDraw.ImageDraw, start: float) -> None:
    steps = 600
    for i in range(steps + 1):
        t = i / steps
        a = start + ARC * t
        w = W_TAIL + (W_HEAD - W_TAIL) * t**1.4
        x, y = point(a, radius_at(t * ARC / (ARC + HEAD_LEN)))
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=255)
    # curved arrowhead: wide base at the shaft's end, tip further along the loop
    base = start + ARC
    half = W_HEAD * 1.05
    edge_out, edge_in = [], []
    for i in range(41):
        t = i / 40
        a = base + HEAD_LEN * t
        r_half = half * (1 - t)
        rc = radius_at((ARC + HEAD_LEN * t) / (ARC + HEAD_LEN))
        edge_out.append(point(a, rc + r_half))
        edge_in.append(point(a, rc - r_half))
    d.polygon(edge_out + edge_in[::-1], fill=255)


def rainbow(size: int) -> Image.Image:
    """A conic gradient: hue follows the angle around the centre."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    ang = (np.arctan2(xx - size / 2, -(yy - size / 2)) / (2 * np.pi)) % 1.0
    lut = np.array(
        [colorsys.hsv_to_rgb(h / 1024, 0.85, 1.0) for h in range(1024)], dtype=np.float32
    )
    rgb = lut[(ang * 1023).astype(np.int32)] * 255
    return Image.fromarray(rgb.astype(np.uint8), "RGB")


def main() -> None:
    mask = Image.new("L", (SIZE, SIZE), 0)
    d = ImageDraw.Draw(mask)
    for k in range(2):
        draw_arrow(d, math.radians(8) + k * math.pi)
    mask = mask.filter(ImageFilter.GaussianBlur(SS * 0.6))
    icon = rainbow(SIZE).convert("RGBA")
    icon.putalpha(mask)
    out = Path(__file__).parents[1] / "custom_components" / "homeo" / "brand"
    out.mkdir(parents=True, exist_ok=True)
    icon.resize((512, 512), Image.LANCZOS).save(out / "icon@2x.png")
    icon.resize((256, 256), Image.LANCZOS).save(out / "icon.png")


if __name__ == "__main__":
    main()
