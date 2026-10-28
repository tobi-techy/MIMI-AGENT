"""Count a shelf photo: SAM finds the packs, code says what each one is and
what the shelf is worth.

``count_shelf(photo, candidates, prices, segmenter)``:

1. ``check_photo`` (code). A bad photo stops here: no SAM call, no count.
2. SAM 3.1 with two concept phrases, ``"product"`` and ``"box"`` (one call
   each), and ``"package"`` when those two find under three packs: every
   pack's box and SAM's own confidence.
3. Code keeps boxes at or above ``MIN_SCORE`` and of a sane size, drops
   near-duplicates (IoU >= 0.6 with a better box) and a box that wraps two or
   more others (a whole stack seen as one product).
4. Code puts each box with an item by the pack's body colour (a light-tolerant
   match against ``mimi.credit.looks``) and its shape (height over width).
   Only the shop's own delivered items (the candidate list) are counted. A box
   that best fits an item Coral Lane never delivered to this shop, or fits
   nothing, is "other stock": seen, never counted, never valued.
5. Value per item = pieces x the distributor's unit price / pieces per unit,
   integer kobo, rounded down. The model never touches a count or a price.

Counts are always "about": each item carries a confidence band from SAM's
scores, how well the colours fit and the photo's sharpness.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from PIL import Image

from mimi.credit.looks import LOOKS, Look
from mimi.credit.photo import PhotoCheck, check_photo, decode
from mimi.credit.sam import Box, Segmenter

# SAM concept phrases, one call each; boxes are merged. "product" finds most
# packs; "box" finds the ones "product" misses on some shelves (set on dev).
PROMPTS: tuple[str, ...] = ("product", "box")
# On some shelves both find almost nothing; then one more phrase (dev).
FALLBACK_PROMPTS: tuple[str, ...] = ("package",)
FALLBACK_BELOW = 3
MIN_SCORE = 0.35
MIN_AREA_FRAC = 0.0012
MAX_AREA_FRAC = 0.2
NMS_IOU = 0.6
# Colour fit: share of a box's pixels whose colour matches the pack body.
MIN_FIT = 0.38
# How far a pixel may sit from the body colour after allowing for light.
COLOUR_TOL = 0.2
ASPECT_SLACK = (0.78, 1.28)

Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class ItemCount:
    sku: str
    name: str
    piece: str
    pieces_word: str
    about: int
    confidence: Confidence
    unit_price_kobo: int
    pieces_per_unit: int
    value_kobo: int
    mean_score: float
    mean_fit: float


@dataclass(frozen=True)
class ShelfCount:
    photo_id: str
    check: PhotoCheck
    items: tuple[ItemCount, ...] = ()
    other_pieces: int = 0
    boxes_seen: int = 0
    total_value_kobo: int = 0
    confidence: Confidence = "low"
    sam_images: int = 0
    notes: tuple[str, ...] = field(default=())

    @property
    def usable(self) -> bool:
        return self.check.usable

    def counts(self) -> dict[str, int]:
        return {i.sku: i.about for i in self.items}


def photo_id_for(data: bytes) -> str:
    return "ph_" + hashlib.sha256(data).hexdigest()[:12]


def _fit(img: Image.Image, box: Box, look: Look) -> float:
    """Share of the box's pixels that match the look's body colour (any light)."""
    pad_x, pad_y = box.w // 10, box.h // 12
    crop = img.crop((box.x1 + pad_x, box.y1 + pad_y, box.x2 - pad_x, box.y2 - pad_y))
    crop = crop.resize((20, 20))
    r0, g0, b0 = look.body_rgb
    rr = r0 * r0 + g0 * g0 + b0 * b0
    hit = 0
    raw = crop.tobytes()
    pixels = list(zip(raw[0::3], raw[1::3], raw[2::3], strict=True))
    for r, g, b in pixels:
        k = (r * r0 + g * g0 + b * b0) / rr
        if k < 0.45 or k > 1.45:
            continue
        dr, dg, db = r - k * r0, g - k * g0, b - k * b0
        if (dr * dr + dg * dg + db * db) <= (COLOUR_TOL * COLOUR_TOL) * rr * k * k:
            hit += 1
    return hit / len(pixels)


def _aspect_ok(box: Box, look: Look) -> bool:
    a = box.h / box.w
    lo, hi = look.aspect
    return lo * ASPECT_SLACK[0] <= a <= hi * ASPECT_SLACK[1]


def keep_boxes(boxes: Sequence[Box], frame: tuple[int, int]) -> list[Box]:
    fw, fh = frame
    area = fw * fh
    ok = [b for b in boxes if b.score >= MIN_SCORE
          and MIN_AREA_FRAC * area <= b.area <= MAX_AREA_FRAC * area]
    ok.sort(key=lambda b: -b.score)
    kept: list[Box] = []
    for b in ok:
        if all(b.iou(k) < NMS_IOU for k in kept):
            kept.append(b)
    # A box that wraps two or more kept boxes is a group, not one pack.
    out = []
    for b in kept:
        inside = 0
        for o in kept:
            if o is b:
                continue
            ix = max(0, min(b.x2, o.x2) - max(b.x1, o.x1))
            iy = max(0, min(b.y2, o.y2) - max(b.y1, o.y1))
            if ix * iy >= 0.8 * o.area:
                inside += 1
        if inside < 2:
            out.append(b)
    return out


def classify(img: Image.Image, box: Box, looks: Mapping[str, Look]) -> tuple[str, float]:
    """The best-fitting item for one box, or ("", fit) when nothing fits."""
    best, best_fit = "", 0.0
    for sku, look in looks.items():
        if not _aspect_ok(box, look):
            continue
        f = _fit(img, box, look)
        if f > best_fit:
            best, best_fit = sku, f
    return (best, best_fit) if best_fit >= MIN_FIT else ("", best_fit)


def _band(score: float, fit: float, sharp: float) -> Confidence:
    if score >= 0.6 and fit >= 0.4 and sharp >= 150:
        return "high"
    if score >= 0.45 and fit >= 0.3:
        return "medium"
    return "low"


def count_shelf(
    photo: bytes,
    candidates: Sequence[str],
    prices: Mapping[str, tuple[int, int, str, str]],
    segmenter: Segmenter,
    *,
    mime: str = "image/jpeg",
    prompts: Sequence[str] = PROMPTS,
) -> ShelfCount:
    """``prices``: sku -> (unit_price_kobo, pieces_per_unit, name, unit) from the book."""
    pid = photo_id_for(photo)
    check = check_photo(photo)
    if not check.usable:
        return ShelfCount(pid, check)
    img = decode(photo)
    assert img is not None
    raw: list[Box] = []
    billed = 0
    for prompt in prompts:
        seg = segmenter.segment(photo, mime, prompt)
        raw.extend(seg.boxes)
        billed += seg.images_billed
    boxes = keep_boxes(raw, img.size)
    if len(boxes) < FALLBACK_BELOW:
        for prompt in FALLBACK_PROMPTS:
            seg = segmenter.segment(photo, mime, prompt)
            raw.extend(seg.boxes)
            billed += seg.images_billed
        boxes = keep_boxes(raw, img.size)
    per: dict[str, list[tuple[float, float]]] = {}
    other = 0
    cand = set(candidates)
    for box in boxes:
        sku, fit = classify(img, box, LOOKS)
        if not sku or sku not in cand or sku not in prices:
            other += 1
            continue
        per.setdefault(sku, []).append((box.score, fit))
    items: list[ItemCount] = []
    for sku in sorted(per, key=lambda s: (-len(per[s]), s)):
        rows = per[sku]
        n = len(rows)
        score = sum(r[0] for r in rows) / n
        fit = sum(r[1] for r in rows) / n
        price, pieces, name, _unit = prices[sku]
        look = LOOKS[sku]
        items.append(ItemCount(
            sku=sku, name=name, piece=look.piece, pieces_word=look.pieces_word, about=n,
            confidence=_band(score, fit, check.sharpness), unit_price_kobo=price,
            pieces_per_unit=pieces, value_kobo=(n * price) // pieces,
            mean_score=round(score, 3), mean_fit=round(fit, 3)))
    total = sum(i.value_kobo for i in items)
    bands = [i.confidence for i in items]
    overall: Confidence = ("low" if not items or "low" in bands
                           else "medium" if "medium" in bands else "high")
    notes = []
    if other:
        notes.append(f"{other} other pieces seen that this distributor did not deliver; not counted")
    return ShelfCount(pid, check, tuple(items), other, len(boxes), total, overall,
                      billed, tuple(notes))
