"""Synthetic handwritten order lists for the order eval (RAI-302).

    python -m mimi.orders.fixtures          # (re)draw eval/order/photos/*.jpg
    python -m mimi.orders.fixtures --check  # the committed photos match this code

Each photo is a phone shot of a page torn from a notebook: ruled paper, blue
or black biro in a script typeface, each line a little tilted, the page
turned a few degrees, soft shadow and camera noise. Every line is written
here, so the eval knows exactly what the page says. No real shop, person or
handwriting is in any photo. Fonts are the URW base35 set that ships with
Ghostscript (Z003 and URW Bookman), so the drawing is the same on any
machine that has them; ``--check`` compares hashes, recorded in
``eval/order/photos.jsonl``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "eval" / "order" / "photos"
INDEX = ROOT / "eval" / "order" / "photos.jsonl"

FONTS = (
    "/usr/share/fonts/opentype/urw-base35/Z003-MediumItalic.otf",
    "/usr/share/fonts/opentype/urw-base35/URWBookman-LightItalic.otf",
)

# id -> (heading, lines, ink, font index). What the page says, exactly.
LISTS: dict[str, tuple[str, tuple[str, ...], str, int]] = {
    # held-out
    "ol-01": ("Order - Coral Lane", ("5 ctn Indomie (big)", "3 ctn Tin milk",
                                     "10 pk spaghetti"), "blue", 0),
    "ol-02": ("To Coral Lane", ("Sugar 50kg - 2 bags", "Cubes - 1 ctn", "Toothpaste - 12"),
              "black", 1),
    "ol-03": ("my order", ("Sachet milk x6 bags", "Omo small x 10", "Semo 5"), "blue", 1),
    "ol-04": ("Next van", ("Peak milk tin 2 carton", "Vegetable oil 5L - 3", "Biscuit 4 ctn"),
              "black", 0),
    "ol-05": ("List", ("Indomie super pack - 2 ctn", "pure water 5 bag"), "blue", 0),
    "ol-06": ("Order", ("Rice - 5 bags", "Spaghetti ctn - 3"), "black", 1),
    # dev (tuning only)
    "od-01": ("order", ("Spaghetti ctn 1", "Sachet milk 2 bags"), "blue", 1),
    "od-02": ("Coral Lane", ("Rice 50kg x 1", "Cubes 2 pk"), "black", 0),
}

INK = {"blue": (28, 52, 140), "black": (30, 30, 34)}


def draw(pid: str) -> bytes:
    heading, lines, ink, font_i = LISTS[pid]
    return draw_page(f"order-list:{pid}", heading, lines, ink, font_i)


def draw_page(seed: str, heading: str, lines: tuple[str, ...], ink: str = "blue",
              font_i: int = 0) -> bytes:
    """One handwritten notebook page, photographed (the same drawing for a seed)."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    rnd = random.Random(seed)
    w, h = 900, 1200
    page = Image.new("RGB", (w, h), (247, 244, 232))
    d = ImageDraw.Draw(page)
    for y in range(150, h, 70):  # ruled lines and the margin
        d.line([(0, y), (w, y)], fill=(176, 196, 222), width=2)
    d.line([(110, 0), (110, h)], fill=(222, 150, 150), width=2)
    font = ImageFont.truetype(FONTS[font_i], 58)
    head = ImageFont.truetype(FONTS[font_i], 64)
    colour = INK[ink]

    def write(text: str, y: int, f: ImageFont.FreeTypeFont) -> None:
        layer = Image.new("RGBA", (w, 110), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((130 + rnd.randint(-8, 14), 18), text, font=f,
                                   fill=(*colour, 235))
        layer = layer.rotate(rnd.uniform(-2.2, 2.2), resample=Image.Resampling.BICUBIC)
        page.paste(layer, (0, y), layer)

    write(heading, 60, head)
    for n, text in enumerate(lines):
        write(text, 150 + 70 * (n + 1) - 62, font)
    page = page.filter(ImageFilter.GaussianBlur(0.6))
    # The phone shot: a table behind the page, the page turned a little.
    bg = Image.new("RGB", (1000, 1333), (96, 74, 58))
    turned = page.rotate(rnd.uniform(-4.0, 4.0), expand=True, fillcolor=(96, 74, 58),
                         resample=Image.Resampling.BICUBIC)
    bg.paste(turned, ((1000 - turned.width) // 2, (1333 - turned.height) // 2))
    shade = Image.linear_gradient("L").resize(bg.size).rotate(rnd.choice((0, 90, 180)))
    bg = Image.composite(bg, Image.new("RGB", bg.size, (60, 50, 40)),
                         shade.point(lambda v: 205 + v // 5))
    px = bg.load()
    assert px is not None
    for _ in range(9000):  # camera noise
        x, y = rnd.randrange(bg.width), rnd.randrange(bg.height)
        r, g, b = px[x, y]  # type: ignore[misc]
        k = rnd.randint(-14, 14)
        px[x, y] = (max(0, min(255, r + k)), max(0, min(255, g + k)), max(0, min(255, b + k)))
    buf = io.BytesIO()
    bg.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


def rows() -> list[dict[str, object]]:
    return [{"id": pid, "file": f"photos/{pid}.jpg", "heading": v[0], "lines": list(v[1]),
             "split": "dev" if pid.startswith("od-") else "heldout"}
            for pid, v in LISTS.items()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        want = {r["id"]: r for r in map(json.loads, INDEX.read_text().splitlines())}
        bad = [pid for pid, r in want.items()
               if hashlib.sha256((OUT / f"{pid}.jpg").read_bytes()).hexdigest() != r["sha256"]]
        print("photos match" if not bad else f"changed: {bad}")
        return 1 if bad else 0
    OUT.mkdir(parents=True, exist_ok=True)
    out = []
    for r in rows():
        data = draw(str(r["id"]))
        (OUT / f"{r['id']}.jpg").write_bytes(data)
        out.append({**r, "sha256": hashlib.sha256(data).hexdigest(), "width": 1000,
                    "height": 1333})
    INDEX.write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in out))
    print(f"wrote {len(out)} photos to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
