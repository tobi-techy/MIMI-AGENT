"""Draw synthetic shelf photos with known counts (eval fixtures and the sim).

Every image is synthetic and every brand invented. A ``ShelfSpec`` lists the
rows of a shop's shelf; each row holds runs of one item, either one of the
shop's delivered items (``mimi.credit.looks``) or "other stock" the shop
bought elsewhere (``OTHER_LOOKS``: invented packs Coral Lane never sold,
which must never be counted). The renderer adds what a phone photo has: a
wall and wooden shelves, price tags, uneven light, a slight tilt, sensor
noise, JPEG compression, and, for the unhappy path, heavy blur or a dark
room.

``render(spec)`` returns the JPEG bytes and the true number of pieces per
item. Pillow only (no numpy), deterministic for the spec's seed with the
same Pillow version.
"""

from __future__ import annotations

import io
import random
from collections import Counter
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from mimi.credit.looks import LOOKS, Look

W, H = 1280, 960

# Packs a shop may also stock that Coral Lane never delivered (invented).
OTHER_LOOKS: dict[str, Look] = {
    look.sku: look
    for look in (
        Look("OTHER-GREEN-BTL", "bottle", "bottles", "bottle", (40, 120, 60), (2.4, 3.6)),
        Look("OTHER-PINK-PKT", "packet", "packets", "packet", (222, 96, 160), (1.05, 1.6)),
        Look("OTHER-GREY-TIN", "tin", "tins", "can", (150, 150, 158), (1.05, 1.6)),
    )
}
ALL_LOOKS: dict[str, Look] = {**LOOKS, **OTHER_LOOKS}

BRAND_TEXT: dict[str, str] = {
    "NDL-70-CTN": "SUNRISE", "NDL-120-CTN": "SUNRISE XL", "SPAG-500": "MAMALAND",
    "MLK-TIN-CTN": "OKIN", "SARD-125": "SEAVIEW", "MALT-33CL": "ROYAL",
    "OIL-1L": "PALMSIDE", "SODA-50CL": "FIZZUP", "DET-900G": "BRIGHTWASH",
    "TPASTE-140": "SMILE", "CUSTARD-2KG": "MAMALAND", "OTHER-GREEN-BTL": "KOLA",
    "OTHER-PINK-PKT": "BELLA", "OTHER-GREY-TIN": "TASTY",
}
# Base piece size in pixels (width, height) at row scale 1.0.
BASE_SIZE: dict[str, tuple[int, int]] = {
    "NDL-70-CTN": (96, 124), "NDL-120-CTN": (112, 146), "SPAG-500": (64, 168),
    "MLK-TIN-CTN": (78, 104), "SARD-125": (124, 78), "MALT-33CL": (62, 122),
    "OIL-1L": (66, 196), "SODA-50CL": (58, 178), "DET-900G": (104, 140),
    "TPASTE-140": (140, 66), "CUSTARD-2KG": (138, 116),
    "OTHER-GREEN-BTL": (60, 182), "OTHER-PINK-PKT": (96, 124), "OTHER-GREY-TIN": (78, 104),
}


@dataclass(frozen=True)
class Run:
    sku: str
    count: int
    # Pieces stacked on top of each other (packets and tins only).
    stack: int = 1


@dataclass(frozen=True)
class ShelfSpec:
    seed: int
    rows: tuple[tuple[Run, ...], ...]
    # "ok", "blurry", "dark"
    quality: str = "ok"
    tilt_deg: float = 0.0
    scale: float = 1.0
    light: float = 1.0
    wall: tuple[int, int, int] = (206, 196, 178)
    # Space between packs in pixels (negative: packs overlap, a crowded shelf).
    gap: tuple[int, int] = (3, 16)
    # Camera softness on a usable photo (the blurry case uses much more).
    blur: float = 0.7
    notes: tuple[str, ...] = field(default=())


def _shade(rgb: tuple[int, int, int], f: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(c * f))) for c in rgb)  # type: ignore[return-value]


def _jitter(rgb: tuple[int, int, int], rng: random.Random, amount: int = 10
            ) -> tuple[int, int, int]:
    return tuple(max(0, min(255, c + rng.randint(-amount, amount))) for c in rgb)  # type: ignore[return-value]


def _piece(sku: str, w: int, h: int, rng: random.Random) -> Image.Image:
    """One pack, drawn upright on a transparent tile."""
    look = ALL_LOOKS[sku]
    body = _jitter(look.body_rgb, rng, 8)
    dark = _shade(body, 0.62)
    light = _shade(body, 1.25)
    tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    text = BRAND_TEXT.get(sku, "")
    if look.shape == "packet":
        d.rounded_rectangle([1, 6, w - 2, h - 7], radius=max(4, w // 10), fill=body,
                            outline=dark, width=2)
        for x in range(2, w - 2, 6):  # crimped seals top and bottom
            d.line([(x, 1), (x + 3, 7)], fill=dark, width=2)
            d.line([(x, h - 2), (x + 3, h - 8)], fill=dark, width=2)
        d.rectangle([4, int(h * 0.12), w - 5, int(h * 0.18)], fill=light)
        d.ellipse([int(w * 0.18), int(h * 0.42), int(w * 0.82), int(h * 0.78)],
                  fill=(246, 214, 112) if sku != "SPAG-500" else (236, 236, 226))
        d.text((6, int(h * 0.24)), text[:9], fill=(250, 250, 250))
    elif look.shape == "can":
        cap = max(6, h // 9)
        d.rectangle([0, cap // 2, w - 1, h - cap // 2], fill=body)
        d.ellipse([0, h - cap, w - 1, h - 1], fill=dark)
        d.ellipse([0, 0, w - 1, cap], fill=(196, 198, 204), outline=(120, 120, 126))
        d.rectangle([int(w * 0.12), cap, int(w * 0.22), h - cap], fill=light)
        d.rectangle([0, int(h * 0.42), w - 1, int(h * 0.6)], fill=(236, 236, 232))
        d.text((5, int(h * 0.45)), text[:8], fill=dark)
    elif look.shape == "bottle":
        neck = max(10, int(w * 0.36))
        x0 = (w - neck) // 2
        d.rectangle([x0, 0, x0 + neck, int(h * 0.07)], fill=(196, 32, 32))
        d.rectangle([x0 + 2, int(h * 0.07), x0 + neck - 2, int(h * 0.2)], fill=body)
        d.polygon([(x0 + 2, int(h * 0.2)), (x0 + neck - 2, int(h * 0.2)),
                   (w - 1, int(h * 0.34)), (0, int(h * 0.34))], fill=body)
        d.rounded_rectangle([0, int(h * 0.33), w - 1, h - 1], radius=max(4, w // 7),
                            fill=body)
        d.rectangle([int(w * 0.1), int(h * 0.36), int(w * 0.2), h - 8], fill=light)
        band = (238, 238, 230) if sku not in ("SODA-50CL",) else (206, 30, 36)
        d.rectangle([2, int(h * 0.5), w - 3, int(h * 0.72)], fill=band)
        d.text((4, int(h * 0.56)), text[:7], fill=dark if band[0] > 220 else (255, 255, 255))
    elif look.shape == "box":
        # A carton seen a little from above: top face, front face, end face.
        top = max(6, h // 4)
        end = max(6, w // 9)
        d.polygon([(0, top), (end, 0), (w - 1, 0), (w - 1 - end, top)], fill=light,
                  outline=dark)
        d.polygon([(w - 1 - end, top), (w - 1, 0), (w - 1, h - 1 - top), (w - 1 - end, h - 1)],
                  fill=dark)
        d.rectangle([0, top, w - 1 - end, h - 1], fill=body, outline=dark, width=2)
        d.rectangle([int(w * 0.5), top + 3, w - end - 5, h - 4], fill=(240, 240, 236))
        d.text((6, top + (h - top) // 2 - 6), text[:8], fill=(250, 250, 250))
    elif look.shape == "tub":
        d.polygon([(int(w * 0.06), int(h * 0.16)), (int(w * 0.94), int(h * 0.16)),
                   (int(w * 0.86), h - 1), (int(w * 0.14), h - 1)], fill=body, outline=dark)
        d.rectangle([0, 0, w - 1, int(h * 0.17)], fill=_shade(body, 0.8), outline=dark)
        d.ellipse([int(w * 0.3), int(h * 0.35), int(w * 0.7), int(h * 0.8)],
                  fill=(210, 60, 40))
        d.text((int(w * 0.2), int(h * 0.22)), text[:8], fill=dark)
    else:  # pouch
        d.rounded_rectangle([0, int(h * 0.08), w - 1, h - 1], radius=max(6, w // 6),
                            fill=body, outline=dark, width=2)
        d.rectangle([2, 0, w - 3, int(h * 0.1)], fill=_shade(body, 0.8))
        d.ellipse([int(w * 0.2), int(h * 0.3), int(w * 0.8), int(h * 0.7)],
                  fill=(250, 250, 250))
        d.text((6, int(h * 0.75)), text[:10], fill=(250, 250, 250))
    return tile


def _background(spec: ShelfSpec, rng: random.Random, shelf_ys: list[int]) -> Image.Image:
    img = Image.new("RGB", (W, H), spec.wall)
    d = ImageDraw.Draw(img)
    for y in range(0, H, 3):  # plaster texture
        f = 0.94 + 0.08 * rng.random()
        d.line([(0, y), (W, y)], fill=_shade(spec.wall, f), width=3)
    for sy in shelf_ys:
        wood = (128 + rng.randint(-10, 10), 86 + rng.randint(-8, 8), 50)
        d.rectangle([0, sy, W, sy + 24], fill=wood)
        for x in range(0, W, 37):
            d.line([(x, sy + 3), (x + 24, sy + 20)], fill=_shade(wood, 0.85), width=1)
        d.rectangle([0, sy + 24, W, sy + 32], fill=_shade(wood, 0.6))
    return img


def render(spec: ShelfSpec) -> tuple[bytes, dict[str, int]]:
    """The photo (JPEG bytes) and the true pieces per item."""
    rng = random.Random(spec.seed)
    n_rows = len(spec.rows)
    row_h = (H - 40) // n_rows
    shelf_ys = [20 + row_h * (i + 1) - 30 for i in range(n_rows)]
    img = _background(spec, rng, shelf_ys)
    draw = ImageDraw.Draw(img)
    truth: Counter[str] = Counter()
    for r, row in enumerate(spec.rows):
        base_y = shelf_ys[r]
        tallest = max(BASE_SIZE[run.sku][1] * run.stack for run in row)
        fit = min(1.0, (row_h - 50) / tallest) * spec.scale
        x = 14 + rng.randint(0, 20)
        for run in row:
            bw, bh = BASE_SIZE[run.sku]
            for _ in range(run.count):
                s = fit * rng.uniform(0.94, 1.06)
                pw, ph = max(12, int(bw * s)), max(12, int(bh * s))
                if x + pw > W - 10:
                    break
                for level in range(run.stack):
                    tile = _piece(run.sku, pw, ph, rng)
                    if rng.random() < 0.5:
                        tile = tile.rotate(rng.uniform(-3.5, 3.5), expand=True,
                                           resample=Image.Resampling.BICUBIC)
                    y = base_y - ph * (level + 1) - level * 2 + rng.randint(-2, 1)
                    # a soft contact shadow behind the pack
                    shadow = Image.new("RGBA", tile.size, (0, 0, 0, 0))
                    shadow.paste((40, 30, 20, 90), (0, 0), tile.split()[-1])
                    shadow = shadow.filter(ImageFilter.GaussianBlur(3))
                    img.paste(shadow, (x + 4, max(0, y + 3)), shadow)
                    img.paste(tile, (x, max(0, y)), tile)
                    truth[run.sku] += 1
                x += pw + rng.randint(*spec.gap)
            x += rng.randint(10, 40)
        # price tags on the shelf edge
        for tx in range(60, W - 80, rng.randint(240, 340)):
            draw.rectangle([tx, base_y + 4, tx + 56, base_y + 20], fill=(250, 250, 244))
            draw.text((tx + 4, base_y + 6), f"N{rng.randint(4, 39) * 100}", fill=(30, 30, 30))

    # light: brighter on one side, a soft vignette
    mask = Image.linear_gradient("L").rotate(90 if rng.random() < 0.5 else -90).resize((W, H))
    shade = Image.new("RGB", (W, H), (0, 0, 0))
    img = Image.composite(img, shade, ImageOps.autocontrast(mask, cutoff=0).point(
        lambda v: 150 + v * 105 // 255))
    img = ImageEnhance.Brightness(img).enhance(spec.light)
    if spec.tilt_deg:
        img = img.rotate(spec.tilt_deg, resample=Image.Resampling.BICUBIC, expand=False,
                         fillcolor=spec.wall)
    noise = Image.effect_noise((W, H), 18).convert("RGB")
    img = Image.blend(img, noise, 0.06)
    if spec.quality == "blurry":
        img = img.filter(ImageFilter.GaussianBlur(7.5))
    elif spec.quality == "dark":
        img = ImageEnhance.Brightness(img).enhance(0.16)
    else:
        img = img.filter(ImageFilter.GaussianBlur(spec.blur))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80, optimize=True)
    return buf.getvalue(), dict(truth)
