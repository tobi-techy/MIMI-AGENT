"""Synthetic photos for the photo routing eval (RAI-304).

    python scripts/make_photo_route_fixtures.py          # (re)draw eval/photo_route/photos/*.jpg
    python scripts/make_photo_route_fixtures.py --check  # the committed photos match this code

Two kinds of camera photo, all drawn here so the eval knows what each says:

* handwritten order lists (the same notebook pages as ``mimi.orders.fixtures``,
  other goods, other shops' wording);
* look-alikes that are not order lists: a POS slip, a printed invoice from the
  distributor, a bank deposit teller, a printed registration certificate and
  a handwritten list of payments (amounts, not goods).

No real shop, bank, person or handwriting is in any photo. Bank names are
invented. Shelf photos and payment screenshots are reused from
``eval/credit/shelves`` and ``eval/proof/screens``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mimi.orders.fixtures import draw_page  # noqa: E402

OUT = ROOT / "eval" / "photo_route" / "photos"
INDEX = ROOT / "eval" / "photo_route" / "photos.jsonl"
PRINT = "/usr/share/fonts/opentype/urw-base35/NimbusSans-Regular.otf"
PRINT_BOLD = "/usr/share/fonts/opentype/urw-base35/NimbusSans-Bold.otf"
MONO = "/usr/share/fonts/opentype/urw-base35/NimbusMonoPS-Regular.otf"

# Handwritten pages: id -> (heading, lines, ink, font index, what it is).
HANDWRITTEN: dict[str, tuple[str, tuple[str, ...], str, int, str]] = {
    # fresh (scored once, never tuned on)
    "fl-01": ("", ("Indomie small 4 ctn", "Milo 500g x 6", "Sugar 2 bag"), "blue", 0, "list"),
    "fl-02": ("Iya Bisi shop", ("Golden morn 3 ctn", "Peak sachet 4 bag"), "black", 1, "list"),
    "fl-03": ("", ("Rice 50kg - 2", "Groundnut oil 5L x 4", "Maggi 1 ctn"), "blue", 1, "list"),
    "fl-04": ("Tuesday", ("Spaghetti 2 ctn", "Tomato paste tin 3 ctn"), "black", 0, "list"),
    "fl-05": ("", ("Semo 10kg - 3", "Detergent big x 5", "Toothpaste 6"), "blue", 0, "list"),
    "fl-06": ("oja", ("Malt can 4 pack", "Coke 50cl 3 pack"), "black", 1, "list"),
    "fl-07": ("Next week", ("Indomie big 6 ctn", "Biscuit 2 ctn"), "blue", 1, "list"),
    "fl-08": ("", ("Tin milk 2 ctn", "Cubes 3 pk", "Salt 1 bag"), "black", 0, "list"),
    "fl-09": ("Coral Lane", ("Pure water 10 bag", "Sachet milk 2 bags"), "blue", 0, "list"),
    "fl-10": ("", ("Noodles 70g - 5 carton", "Custard 2kg x 2"), "black", 1, "list"),
    "fl-11": ("kaya", ("Sugar 50kg 1", "Oil 5L 2", "Rice 5kg 4"), "blue", 1, "list"),
    "fl-12": ("", ("Milo 500g 3", "Golden morn 1 ctn"), "black", 0, "list"),
    "fl-13": ("+ usual", ("Semo 1kg 6 pk", "Spaghetti 500 x 12"), "blue", 0, "list"),
    "fl-14": ("Order", ("Biscuit 3 ctn", "Indomie super pack 2 ctn"), "black", 1, "list"),
    "fl-15": ("", ("Peak tin 1 ctn", "Detergent sachet 10 pk"), "blue", 1, "list"),
    "fl-16": ("", ("Rice 5kg x 3", "Oil 5L x 1"), "black", 0, "list"),
    "fx-paid": ("Paid this week", ("Mon 20,000", "Wed 15,000 transfer", "Sat 30,000 cash"),
                "blue", 1, "payments"),
    # dev (tuning only)
    "dl-01": ("", ("Cubes 1 ctn", "Rice 50kg 1"), "blue", 0, "list"),
    "dl-02": ("for van", ("Malt can 2 pack", "Milo 500g 2"), "black", 1, "list"),
    "dx-paid": ("Money", ("Friday 10,000", "Sunday 25,000"), "black", 0, "payments"),
}

# Printed look-alikes: id -> (style, lines, what it is).
PRINTED: dict[str, tuple[str, tuple[str, ...], str]] = {
    "fx-pos": ("slip", ("KESTREL POS", "PURCHASE / TRANSFER", "APPROVED", "AMOUNT: NGN 45,000.00",
                        "TERMINAL 2093XX81", "REF 004412987311", "26/10/2026 17:42",
                        "THANK YOU"), "pos_slip"),
    "fx-inv": ("sheet", ("CORAL LANE TRADING", "SALES INVOICE  No. 0412", "Customer: shop",
                         "5 ctn Noodles 120g      94,500", "3 ctn Evap milk 160g    78,000",
                         "TOTAL                 172,500", "Goods received in good condition"),
               "invoice"),
    "fx-tel": ("teller", ("HERONGATE BANK - DEPOSIT SLIP", "Account name: Coral Lane Trading",
                          "Account no: 01XXXXXX77", "Amount in figures: 120,000",
                          "Depositor: shop owner", "Date: 27/10/2026"), "teller"),
    "fx-doc": ("sheet", ("CERTIFICATE OF REGISTRATION", "BUSINESS NAME", "This is to certify",
                         "that the business name", "has been registered", "Registrar",
                         "Date: 03/02/2024"), "document"),
    "dx-pos": ("slip", ("FINCH POS", "TRANSFER", "SUCCESSFUL", "AMOUNT: NGN 12,500.00",
                        "REF 0091223"), "pos_slip"),
    "dx-doc": ("sheet", ("TENANCY RECEIPT", "Received from shop", "the sum of", "Rent for 1 year",
                         "Landlord"), "document"),
}


def draw_printed(seed: str, style: str, lines: tuple[str, ...]) -> bytes:
    """A printed slip or sheet on a table, photographed with a phone."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    rnd = random.Random(seed)
    if style == "slip":
        w, h, size, font_path = 520, 1000, 34, MONO
    else:
        w, h, size, font_path = 860, 1150, 36, PRINT
    paper = (250, 250, 246) if style != "teller" else (236, 242, 250)
    page = Image.new("RGB", (w, h), paper)
    d = ImageDraw.Draw(page)
    font = ImageFont.truetype(font_path, size)
    bold = ImageFont.truetype(PRINT_BOLD if style != "slip" else MONO, size + 6)
    y = 60
    for n, text in enumerate(lines):
        f = bold if n == 0 else font
        d.text((40, y), text, font=f, fill=(25, 25, 30))
        y += size + 34
        if style == "teller" and n:
            d.line([(40, y - 14), (w - 40, y - 14)], fill=(150, 160, 190), width=1)
    if style == "sheet":
        d.rectangle([(30, 30), (w - 30, h - 30)], outline=(60, 60, 70), width=3)
    page = page.filter(ImageFilter.GaussianBlur(0.7))
    bg = Image.new("RGB", (1000, 1333), (92, 72, 56))
    turned = page.rotate(rnd.uniform(-5.0, 5.0), expand=True, fillcolor=(92, 72, 56),
                         resample=Image.Resampling.BICUBIC)
    bg.paste(turned, ((1000 - turned.width) // 2, (1333 - turned.height) // 2))
    px = bg.load()
    assert px is not None
    for _ in range(9000):
        x, yy = rnd.randrange(bg.width), rnd.randrange(bg.height)
        r, g, b = px[x, yy]  # type: ignore[misc]
        k = rnd.randint(-14, 14)
        px[x, yy] = (max(0, min(255, r + k)), max(0, min(255, g + k)), max(0, min(255, b + k)))
    buf = io.BytesIO()
    bg.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


def rows() -> list[tuple[dict[str, object], bytes]]:
    out: list[tuple[dict[str, object], bytes]] = []
    for pid, (heading, lines, ink, font_i, what) in HANDWRITTEN.items():
        data = draw_page(f"photo-route:{pid}", heading, lines, ink, font_i)
        out.append(({"id": pid, "file": f"photos/{pid}.jpg", "what": what,
                     "says": [heading, *lines] if heading else list(lines)}, data))
    for pid, (style, lines, what) in PRINTED.items():
        data = draw_printed(f"photo-route:{pid}", style, lines)
        out.append(({"id": pid, "file": f"photos/{pid}.jpg", "what": what,
                     "says": list(lines)}, data))
    for r, data in out:
        r["split"] = "dev" if str(r["id"]).startswith("d") else "fresh"
        r["sha256"] = hashlib.sha256(data).hexdigest()
        r["width"], r["height"] = 1000, 1333
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    got = rows()
    if a.check:
        want = {r["id"]: r for r in map(json.loads, INDEX.read_text().splitlines())}
        bad = [str(r["id"]) for r, _ in got
               if want.get(str(r["id"]), {}).get("sha256") != r["sha256"]
               or hashlib.sha256((OUT / f"{r['id']}.jpg").read_bytes()).hexdigest()
               != r["sha256"]]
        print("photos match" if not bad else f"changed: {bad}")
        return 1 if bad else 0
    OUT.mkdir(parents=True, exist_ok=True)
    for r, data in got:
        (OUT / f"{r['id']}.jpg").write_bytes(data)
    INDEX.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r, _ in got))
    print(f"wrote {len(got)} photos to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
