"""Draw synthetic payment screenshots with Pillow (eval fixtures and the sim).

Mobile bank-app transfer receipts and SMS debit alerts for the fictional
banks in ``banks.py``, with invented logos drawn from plain shapes (no real
bank's name, logo or layout), a phone status bar, and mild realism: JPEG
compression, crops, a little rotation or blur. Fakes are made the way
people make them: an amount typed over in another font, a wrong recipient,
a reused reference, an impossible time, a "Pending" under a big success
tick, an old receipt sent again.

``render(spec)`` returns the JPEG bytes and ``truth``: the fields exactly as
printed (what a perfect reader would copy), which the fake reader and the
eval labels use. Pillow is a dev/sim dependency only; Mimi itself never
draws or stores an image.
"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from mimi.proof.banks import CORAL_LANE_ACCOUNT, FictionalBank, bank_named
from mimi.proof.reading import TYPEFACE_FIELDS

FONT_DIRS = (
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/dejavu"),
    Path("/usr/share/fonts/opentype/urw-base35"),
)
FONT_FILES = {
    "sans": "DejaVuSans.ttf",
    "bold": "DejaVuSans-Bold.ttf",
    "serif": "DejaVuSerif.ttf",
    "serif_bold": "DejaVuSerif-Bold.ttf",
    "mono": "DejaVuSansMono.ttf",
    "condensed": "DejaVuSansCondensed.ttf",
}

FontT = ImageFont.FreeTypeFont | ImageFont.ImageFont


@lru_cache(maxsize=64)
def font(kind: str, size: int) -> FontT:
    name = FONT_FILES.get(kind, FONT_FILES["sans"])
    for d in FONT_DIRS:
        p = d / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


BRAND_COLOR: dict[str, tuple[int, int, int]] = {
    "Kestrel Bank": (75, 42, 138),
    "Harmattan MFB": (214, 112, 24),
    "Lagoonpay": (14, 124, 134),
    "Ironleaf Bank": (47, 107, 58),
    "Ashgrove Bank": (88, 96, 112),
    "Marlwood Bank": (128, 38, 52),
}
BRAND_LOGO = {"Kestrel Bank": "kestrel", "Harmattan MFB": "sun", "Lagoonpay": "wave",
              "Ironleaf Bank": "leaf", "Ashgrove Bank": "tree", "Marlwood Bank": "diamond"}


def naira_printed(kobo: int, style: str = "sign") -> str:
    whole, part = divmod(kobo, 100)
    body = f"{whole:,}.{part:02d}"
    return {"sign": "\u20a6" + body, "ngn": "NGN" + body, "n": "N" + body}[style]


def mask_account(number: str, style: int = 0) -> str:
    if style == 1:
        return f"{number[:3]}****{number[-4:]}"
    return "******" + number[-4:]


@dataclass
class ScreenSpec:
    """One screenshot. Every printed field is explicit, so fakes are just
    specs with a wrong value; ``truth`` is derived from the same spec."""

    bank: str
    amount_kobo: int
    paid_at: datetime
    sender: str
    reference: str
    recipient_name: str = CORAL_LANE_ACCOUNT.name
    recipient_account: str = CORAL_LANE_ACCOUNT.number
    recipient_bank: str = CORAL_LANE_ACCOUNT.bank
    session_id: str = ""
    narration: str = ""
    fee_kobo: int | None = None
    # The total debited; None prints amount + fee when a fee is shown.
    total_kobo: int | None = None
    headline: str = ""
    status_detail: str = "Successful"
    # A printed time that overrides ``paid_at`` (an impossible one).
    time_text: str = ""
    # Fields drawn in another font (field name -> font kind): the edit.
    font_overrides: dict[str, str] = field(default_factory=dict)
    # Draw the amount on an off-colour patch, nudged (a typed-over amount).
    amount_patch: bool = False
    sender_account: str = "9990011234"
    balance_kobo: int = 1_234_055
    status_bar_time: str = ""
    # Realism.
    jpeg_quality: int = 82
    rotate_deg: float = 0.0
    blur: float = 0.0
    crop_top: int = 0
    crop_bottom: int = 0
    scale: float = 1.0
    legibility: float = 0.95
    amount_confidence: float = 0.95
    editing_signs: tuple[str, ...] = ()
    hide_amount: bool = False
    seed: int = 0

    @property
    def fb(self) -> FictionalBank:
        b = bank_named(self.bank)
        assert b is not None, self.bank
        return b

    def printed_time(self) -> str:
        return self.time_text or self.paid_at.strftime(self.fb.time_formats[0])

    def printed_total(self) -> int | None:
        if self.total_kobo is not None:
            return self.total_kobo
        if self.fee_kobo is not None:
            return self.amount_kobo + self.fee_kobo
        return None


def _status_bar(d: ImageDraw.ImageDraw, w: int, when: str, dark: bool) -> None:
    fg = (255, 255, 255) if dark else (20, 20, 20)
    d.text((22, 10), when, font=font("bold", 18), fill=fg)
    # signal bars, wifi arc, battery
    x = w - 130
    for i in range(4):
        d.rectangle([x + i * 7, 26 - i * 4, x + i * 7 + 4, 28], fill=fg)
    d.arc([w - 92, 10, w - 68, 34], 220, 320, fill=fg, width=3)
    d.rectangle([w - 56, 12, w - 22, 28], outline=fg, width=2)
    d.rectangle([w - 53, 15, w - 33, 25], fill=fg)
    d.rectangle([w - 21, 17, w - 18, 23], fill=fg)


def _logo(d: ImageDraw.ImageDraw, kind: str, x: int, y: int, s: int, color: Any) -> None:
    if kind == "kestrel":
        d.polygon([(x, y + s), (x + s // 2, y), (x + s, y + s), (x + s // 2, y + s * 2 // 3)],
                  fill=color)
    elif kind == "sun":
        d.ellipse([x + s // 4, y + s // 4, x + s * 3 // 4, y + s * 3 // 4], fill=color)
        for i in range(8):
            import math

            a = i * math.pi / 4
            cx, cy = x + s / 2, y + s / 2
            d.line([(cx + math.cos(a) * s * 0.32, cy + math.sin(a) * s * 0.32),
                    (cx + math.cos(a) * s * 0.5, cy + math.sin(a) * s * 0.5)],
                   fill=color, width=3)
    elif kind == "wave":
        for k in range(3):
            d.arc([x, y + k * s // 4, x + s // 2, y + s // 2 + k * s // 4], 180, 360, fill=color,
                  width=4)
            d.arc([x + s // 2, y + k * s // 4, x + s, y + s // 2 + k * s // 4], 0, 180,
                  fill=color, width=4)
    elif kind == "tree":
        d.polygon([(x + s // 2, y), (x + s, y + s * 3 // 4), (x, y + s * 3 // 4)], fill=color)
        d.rectangle([x + s // 2 - 3, y + s * 3 // 4, x + s // 2 + 3, y + s], fill=color)
    elif kind == "diamond":
        d.polygon([(x + s // 2, y), (x + s, y + s // 2), (x + s // 2, y + s), (x, y + s // 2)],
                  fill=color)
    else:  # leaf
        d.ellipse([x, y + s // 4, x + s, y + s * 3 // 4], fill=color)
        d.line([(x + 4, y + s // 2), (x + s - 4, y + s // 2)], fill=(255, 255, 255), width=2)


def _tick(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color: Any) -> None:
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    d.line([(cx - r // 2, cy), (cx - r // 8, cy + r // 2.5), (cx + r // 2, cy - r // 3)],
           fill=(255, 255, 255), width=max(3, r // 6))


def _rows_for(spec: ScreenSpec) -> list[tuple[str, str, str]]:
    """(field, label, value) rows in the bank's own order and words."""
    acct = mask_account(spec.recipient_account, 1 if spec.bank == "Lagoonpay" else 0)
    total = spec.printed_total()
    money: list[tuple[str, str, str]] = [("amount", "Amount", naira_printed(spec.amount_kobo))]
    if spec.fee_kobo is not None:
        money.append(("fee", "Fee" if spec.bank != "Harmattan MFB" else "Charges",
                      naira_printed(spec.fee_kobo)))
    if total is not None:
        money.append(("total", "Total debited" if spec.bank != "Harmattan MFB" else "Total",
                      naira_printed(total)))
    status = [("status", "Status", spec.status_detail)] if spec.status_detail else []
    if spec.bank == "Kestrel Bank":
        rows = [
            ("recipient_name", "Recipient", spec.recipient_name),
            ("recipient_account", "Account", acct),
            ("recipient_bank", "Bank", spec.recipient_bank),
            ("sender", "Sender", spec.sender),
            ("narration", "Narration", spec.narration),
            ("reference", "Transaction ref", spec.reference),
            ("session_id", "Session ID", spec.session_id),
            ("time", "Date", spec.printed_time()),
        ]
        return money + status + rows
    if spec.bank == "Harmattan MFB":
        rows = [
            ("recipient_name", "Beneficiary", spec.recipient_name),
            ("recipient_account", "Beneficiary account", acct),
            ("recipient_bank", "Beneficiary bank", spec.recipient_bank),
            ("sender", "From", spec.sender),
            ("narration", "Remark", spec.narration),
            ("reference", "Reference", spec.reference),
            ("time", "Date & time", spec.printed_time()),
        ]
        return rows + status + money
    rows = [
        ("recipient_name", "To", spec.recipient_name),
        ("recipient_account", "Account number", acct),
        ("recipient_bank", "Bank", spec.recipient_bank),
        ("sender", "From", spec.sender),
        ("narration", "Description", spec.narration),
        ("reference", "Transaction No.", spec.reference),
        ("time", "Time", spec.printed_time()),
    ]
    return status + rows + [m for m in money if m[0] != "amount"]


def _app(spec: ScreenSpec, rng: random.Random) -> Image.Image:
    w, h = 540, 1100
    color = BRAND_COLOR[spec.bank]
    bg = (246, 246, 248) if spec.bank != "Lagoonpay" else (240, 247, 247)
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    dark_header = spec.bank in ("Kestrel Bank", "Lagoonpay")
    if dark_header:
        d.rectangle([0, 0, w, 150], fill=color)
    _status_bar(d, w, spec.status_bar_time or spec.paid_at.strftime("%H:%M"), dark_header)
    fg_head = (255, 255, 255) if dark_header else color
    d.text((24, 62), "\u2190", font=font("bold", 28), fill=fg_head)
    _logo(d, BRAND_LOGO[spec.bank], 70, 58, 36, fg_head)
    d.text((116, 62), spec.bank, font=font("bold", 26), fill=fg_head)
    top = 170 if dark_header else 120
    d.rounded_rectangle([20, top, w - 20, h - 110], radius=18, fill=(255, 255, 255))
    pending_tint = (234, 167, 30) if spec.headline.lower().startswith("pend") else (34, 160, 90)
    _tick(d, w // 2, top + 60, 34, pending_tint)
    headline = spec.headline or {
        "Kestrel Bank": "Transfer Successful",
        "Harmattan MFB": "Transaction Successful",
        "Lagoonpay": "Payment sent",
    }[spec.bank]
    hf = font("bold", 26)
    tw = d.textlength(headline, font=hf)
    d.text(((w - tw) / 2, top + 108), headline, font=hf, fill=(30, 30, 30))
    amount_text = naira_printed(spec.amount_kobo)
    af = font(spec.font_overrides.get("amount", "bold"), 46)
    aw = d.textlength(amount_text, font=af)
    ax, ay = (w - aw) / 2, top + 150
    if spec.amount_patch:
        d.rectangle([ax - 8, ay - 2, ax + aw + 8, ay + 56], fill=(252, 252, 249))
        ax += 3
        ay += 2
    if not spec.hide_amount:
        d.text((ax, ay), amount_text, font=af, fill=(20, 20, 20))
    y = top + 235
    d.line([(44, y - 12), (w - 44, y - 12)], fill=(228, 228, 232), width=2)
    for key, label, value in _rows_for(spec):
        if not value:
            continue
        if spec.hide_amount and key in ("amount", "total"):
            continue
        d.text((44, y), label, font=font("sans", 18), fill=(120, 120, 128))
        vf = font(spec.font_overrides.get(key, "bold" if key in ("amount", "total") else "sans"),
                  19 if key not in ("reference", "session_id") else 16)
        vw = d.textlength(value, font=vf)
        if vw > w - 250:
            vf = font(spec.font_overrides.get(key, "condensed"), 15)
            vw = d.textlength(value, font=vf)
        vx = w - 44 - vw
        if key == "amount" and spec.amount_patch:
            d.rectangle([vx - 4, y - 2, w - 40, y + 24], fill=(252, 252, 249))
            vx += 2
        sc = (30, 30, 30)
        if key == "status":
            sc = (200, 120, 0) if value.lower() != "successful" else (34, 140, 80)
        d.text((vx, y), value, font=vf, fill=sc)
        y += 44
    d.rounded_rectangle([60, h - 92, w - 60, h - 40], radius=26, fill=color)
    sf = font("bold", 20)
    lab = "Share receipt"
    d.text(((w - d.textlength(lab, font=sf)) / 2, h - 80), lab, font=sf, fill=(255, 255, 255))
    return img


def _sms_lines(spec: ScreenSpec) -> list[tuple[str, str]]:
    total = spec.printed_total()
    amt = naira_printed(spec.amount_kobo, "ngn")
    lines = [
        ("title", f"{spec.bank} Debit Alert"),
        ("account", f"Acct: {spec.sender_account[:2]}******{spec.sender_account[-4:]}"),
        ("amount", f"Amt: {amt} DR"),
    ]
    if total is not None and spec.fee_kobo is not None:
        lines.append(("fee", f"Fee: {naira_printed(spec.fee_kobo, 'ngn')}"))
    lines += [
        ("narration", f"Desc: TRF TO {spec.recipient_name}/{spec.reference}"),
        ("time", f"Time: {spec.printed_time()}"),
        ("balance", f"Avl Bal: {naira_printed(spec.balance_kobo, 'ngn')}"),
    ]
    if spec.status_detail and spec.status_detail.lower() != "successful":
        lines.insert(1, ("status", f"Status: {spec.status_detail}"))
    return lines


def _sms(spec: ScreenSpec, rng: random.Random) -> Image.Image:
    w, h = 540, 980
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    _status_bar(d, w, spec.status_bar_time or spec.paid_at.strftime("%H:%M"), False)
    d.text((22, 62), "\u2190", font=font("bold", 28), fill=(40, 40, 40))
    color = BRAND_COLOR[spec.bank]
    d.ellipse([66, 54, 112, 100], fill=color)
    _logo(d, BRAND_LOGO[spec.bank], 74, 62, 30, (255, 255, 255))
    d.text((124, 64), spec.bank.split()[0].upper(), font=font("bold", 24), fill=(30, 30, 30))
    d.line([(0, 116), (w, 116)], fill=(230, 230, 230), width=2)
    chip = spec.paid_at.strftime("%a, %d %b") if not spec.time_text else "Today"
    cf = font("sans", 16)
    d.text(((w - d.textlength(chip, font=cf)) / 2, 140), chip, font=cf, fill=(130, 130, 130))
    lines = _sms_lines(spec)
    y0 = 180
    bh = 40 + 34 * len(lines)
    d.rounded_rectangle([24, y0, w - 60, y0 + bh], radius=22, fill=(236, 238, 241))
    y = y0 + 20
    for key, text in lines:
        if spec.hide_amount and key == "amount":
            text = "Amt: "
        f = font(spec.font_overrides.get(key, "bold" if key == "title" else "sans"), 19)
        if d.textlength(text, font=f) > w - 110:
            f = font(spec.font_overrides.get(key, "condensed"), 16)
        x = 44
        if key == "amount" and spec.amount_patch:
            d.rectangle([x + 46, y - 2, x + 300, y + 24], fill=(240, 240, 236))
            x += 2
        d.text((x, y), text, font=f, fill=(25, 25, 25))
        y += 34
    stamp = spec.paid_at.strftime("%H:%M") if not spec.time_text else ""
    d.text((w - 120, y0 + bh + 8), stamp, font=font("sans", 15), fill=(140, 140, 140))
    d.rounded_rectangle([20, h - 70, w - 20, h - 24], radius=22, outline=(210, 210, 210), width=2)
    d.text((44, h - 58), "Text message", font=font("sans", 18), fill=(160, 160, 160))
    return img


def not_a_payment(seed: int = 0) -> Image.Image:
    """A drawn 'photo' of a shop shelf: not a payment at all."""
    rng = random.Random(seed)
    img = Image.new("RGB", (540, 720), (205, 190, 160))
    d = ImageDraw.Draw(img)
    for row in range(4):
        y = 80 + row * 160
        d.rectangle([20, y + 120, 520, y + 132], fill=(120, 90, 60))
        x = 30
        while x < 500:
            bw = rng.randint(40, 80)
            col = tuple(rng.randint(40, 230) for _ in range(3))
            d.rectangle([x, y + rng.randint(10, 50), x + bw, y + 120], fill=col)
            x += bw + 6
    return img


def degrade(img: Image.Image, spec: ScreenSpec) -> bytes:
    if spec.crop_top or spec.crop_bottom:
        img = img.crop((0, spec.crop_top, img.width, img.height - spec.crop_bottom))
    if spec.scale != 1.0:
        img = img.resize((max(1, int(img.width * spec.scale)), max(1, int(img.height * spec.scale))),
                         Image.Resampling.LANCZOS)
    if spec.rotate_deg:
        img = img.rotate(spec.rotate_deg, resample=Image.Resampling.BICUBIC, expand=True,
                         fillcolor=(58, 58, 62))
    if spec.blur:
        img = img.filter(ImageFilter.GaussianBlur(spec.blur))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=spec.jpeg_quality, optimize=True)
    return buf.getvalue()


def truth_of(spec: ScreenSpec) -> dict[str, Any]:
    """The fields as printed: what a perfect reader would copy."""
    fb = spec.fb
    total = spec.printed_total()
    hidden = spec.hide_amount
    if fb.layout == "sms_alert":
        return {
            "is_payment_screenshot": True,
            "layout": "sms_alert",
            "bank_name": spec.bank,
            "headline_status": "Debit Alert",
            "status_detail": (
                spec.status_detail if spec.status_detail.lower() != "successful" else None
            ),
            "amount_as_printed": None if hidden else naira_printed(spec.amount_kobo, "ngn"),
            "fee_as_printed": (
                naira_printed(spec.fee_kobo, "ngn")
                if spec.fee_kobo is not None and total is not None
                else None
            ),
            "total_as_printed": None,
            "sender_name": None,
            "recipient_name": spec.recipient_name,
            "recipient_account_as_printed": None,
            "recipient_bank": None,
            "date_time_as_printed": spec.printed_time(),
            "reference": spec.reference,
            "session_id": None,
            "narration": f"TRF TO {spec.recipient_name}/{spec.reference}",
            "typefaces": _typefaces(spec, sms=True),
            "editing_signs": list(spec.editing_signs),
            "editing_confidence": 0.85 if spec.editing_signs else 0.0,
            "legibility": spec.legibility,
            "amount_confidence": 0.0 if hidden else spec.amount_confidence,
        }
    headline = spec.headline or {
        "Kestrel Bank": "Transfer Successful",
        "Harmattan MFB": "Transaction Successful",
        "Lagoonpay": "Payment sent",
    }[spec.bank]
    return {
        "is_payment_screenshot": True,
        "layout": "app_receipt",
        "bank_name": spec.bank,
        "headline_status": headline,
        "status_detail": spec.status_detail or None,
        "amount_as_printed": None if hidden else naira_printed(spec.amount_kobo),
        "fee_as_printed": None if spec.fee_kobo is None else naira_printed(spec.fee_kobo),
        "total_as_printed": None if (total is None or hidden) else naira_printed(total),
        "sender_name": spec.sender,
        "recipient_name": spec.recipient_name,
        "recipient_account_as_printed": mask_account(
            spec.recipient_account, 1 if spec.bank == "Lagoonpay" else 0
        ),
        "recipient_bank": spec.recipient_bank,
        "date_time_as_printed": spec.printed_time(),
        "reference": spec.reference,
        "session_id": spec.session_id or None,
        "narration": spec.narration or None,
        "typefaces": _typefaces(spec, sms=False),
        "editing_signs": list(spec.editing_signs),
        "editing_confidence": 0.85 if spec.editing_signs else 0.0,
        "legibility": spec.legibility,
        "amount_confidence": 0.0 if hidden else spec.amount_confidence,
    }


_FIELD_NAMES = {"sender": "sender_name", "time": "date_time"}
_FAMILY = {"serif": "serif", "serif_bold": "serif", "mono": "monospace"}


def _typefaces(spec: ScreenSpec, *, sms: bool) -> dict[str, str]:
    """The family each key value is drawn in (what a perfect reader sees)."""
    out = {f: "sans-serif" for f in TYPEFACE_FIELDS}
    out["other_rows"] = "sans-serif"
    if sms:
        out["sender_name"] = "unclear"
    for key, kind in spec.font_overrides.items():
        out[_FIELD_NAMES.get(key, key)] = _FAMILY.get(kind, "sans-serif")
    if spec.hide_amount:
        out["amount"] = "unclear"
    return out


def render(spec: ScreenSpec) -> tuple[bytes, dict[str, Any]]:
    rng = random.Random(spec.seed)
    img = _sms(spec, rng) if spec.fb.layout == "sms_alert" else _app(spec, rng)
    return degrade(img, spec), truth_of(spec)


def render_not_a_payment(seed: int = 0, quality: int = 80) -> tuple[bytes, dict[str, Any]]:
    buf = io.BytesIO()
    not_a_payment(seed).save(buf, "JPEG", quality=quality)
    return buf.getvalue(), {
        "is_payment_screenshot": False,
        "layout": "other",
        "typefaces": {f: "unclear" for f in (*TYPEFACE_FIELDS, "other_rows")},
        "editing_signs": [],
        "editing_confidence": 0.0,
        "legibility": 0.9,
        "amount_confidence": 0.0,
    }
