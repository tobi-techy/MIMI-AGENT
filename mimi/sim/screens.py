"""Shops that pay by screenshot in the simulator (RAI-268).

With ``screenshot_share`` above 0, that share of the shops that pay by
transfer (picked from the seed) send a payment screenshot for the day's
delivery an hour or few after it lands; ``fake_share`` of those send a fake
one instead (an edited amount, a wrong recipient, a "Pending" under a
success tick, a time after the message, or another shop's reference). Some
genuine ones send the same image again a few minutes later.

Genuine payments also land in the sim's stand-in for Coral Lane's bank feed
a couple of minutes after the time on the screenshot; fakes never do. The
screenshots are drawn on the fly (``mimi.proof.render``), fictional banks
only, and each one's printed fields are kept by SHA-256 so the offline fake
reader can answer for it.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from mimi.proof.banks import bank_for
from mimi.world.model import Delivery, FeedCredit, Shop

FAKE_KINDS = ("edited_amount", "wrong_recipient", "pending_as_success", "future_time", "reused_reference")


def _rng(*parts: object) -> random.Random:
    return random.Random("|".join(str(p) for p in parts))


def _reference(bank: str, rng: random.Random) -> str:
    d = lambda n: "".join(str(rng.randint(0, 9)) for _ in range(n))  # noqa: E731
    if bank == "Kestrel Bank":
        return "KST" + d(12)
    if bank == "Harmattan MFB":
        return "HMF-" + "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(10))
    if bank == "Lagoonpay":
        return "999" + d(27)
    # The SMS banks: Ironleaf Bank (IL), Ashgrove Bank (AG), Marlwood Bank (MW).
    return f"{bank_for(bank).short}/TRF/" + d(9)


@dataclass(frozen=True)
class SimScreenshot:
    shop_id: str
    delivery_id: str
    at: datetime
    image: bytes
    truth: dict[str, Any]
    fake_kind: str  # "" when genuine
    amount_kobo: int
    resend: bool = False
    credit: FeedCredit | None = None


def plan_screenshot(
    shop: Shop,
    delivery: Delivery,
    delivered_at: datetime,
    *,
    seed: int,
    fake: bool,
    n: int,
    used_refs: list[str],
) -> list[SimScreenshot]:
    """The screenshot (and any resend) this shop sends, with its bank credit."""
    # Pillow only when a sim asks for screenshots (the dev extra installs it).
    from mimi.proof.render import ScreenSpec, render

    rng = _rng("screenshot", shop.shop_id, seed)
    fb = bank_for(shop.bank)
    at = delivered_at + timedelta(minutes=rng.randint(60, 240), seconds=rng.randint(0, 59))
    paid = at - timedelta(minutes=rng.randint(3, 20), seconds=rng.randint(0, 59))
    ref = _reference(fb.name, rng)
    spec = ScreenSpec(
        bank=fb.name,
        amount_kobo=delivery.total_kobo,
        paid_at=paid,
        sender=shop.owner if fb.name == "Harmattan MFB" else shop.owner.upper(),
        reference=ref,
        session_id=("999" + "".join(str(rng.randint(0, 9)) for _ in range(27)))
        if fb.name == "Kestrel Bank"
        else "",
        narration=delivery.delivery_id,
        fee_kobo=1000 if fb.name == "Kestrel Bank" else None,
        status_bar_time=at.strftime("%H:%M"),
        rotate_deg=rng.choice([0.0, 0.0, 1.5, -2.0]),
        jpeg_quality=rng.choice([82, 70, 60]),
        seed=rng.randint(0, 10**6),
    )
    kind = ""
    if fake:
        kind = FAKE_KINDS[n % len(FAKE_KINDS)]
        if kind == "reused_reference" and not used_refs:
            kind = "wrong_recipient"
        if kind == "edited_amount":
            fee = spec.fee_kobo or 0
            spec = replace(spec, fee_kobo=fee, total_kobo=delivery.total_kobo // 10 + fee,
                           font_overrides={"amount": "serif_bold"}, amount_patch=True,
                           editing_signs=("amount in a different font on a lighter patch",))
        elif kind == "wrong_recipient":
            spec = replace(spec, recipient_account="9990009917")
        elif kind == "pending_as_success":
            spec = replace(spec, status_detail="Pending")
        elif kind == "future_time":
            spec = replace(spec, paid_at=at + timedelta(hours=3, minutes=7))
        else:
            spec = replace(spec, reference=used_refs[0])
    else:
        used_refs.append(spec.reference)
    image, truth = render(spec)
    credit = None
    if not fake:
        credit = FeedCredit(
            credit_id=f"sim-credit-{shop.shop_id}",
            payment_id=f"sim-pay-{shop.shop_id}",
            amount_kobo=delivery.total_kobo,
            received_at=(paid + timedelta(minutes=2)).isoformat(),
            narration=f"NIP/{spec.sender}/{spec.reference}/{delivery.delivery_id}",
        )
    out = [SimScreenshot(shop.shop_id, delivery.delivery_id, at, image, truth, kind,
                         delivery.total_kobo, credit=credit)]
    if not fake and rng.random() < 0.3:
        out.append(replace(out[0], at=at + timedelta(minutes=rng.randint(4, 15)), resend=True,
                           credit=None))
    return out
