#!/usr/bin/env python3
"""Make the 40 synthetic payment screenshots and their labels (RAI-261/262).

    python scripts/make_proof_fixtures.py          # writes eval/proof/

Writes ``eval/proof/screens/<id>.jpg`` and ``eval/proof/screens.jsonl``: one
case per screenshot with the shop, when it arrived, the shop's open invoices
at that moment (from the Coral Lane world), any earlier proofs Mimi had seen,
the fields as printed, and the expected verdict, fields, invoice match and
(for fakes) the rules that should catch it.

Everything is synthetic: fictional banks and logos (``mimi.proof.banks``),
fictional shops and owners from the world, account numbers starting 999.
The split: 20 genuine, 16 fakes (edited amount x3, wrong recipient x2,
duplicated reference x2, impossible time x3, pending shown as success x2,
mismatched fonts x2, old screenshot reused x2) and 4 that cannot be read.
This is the dev split; no held-out split is generated yet.

Deterministic for its seed, but JPEG bytes can differ across Pillow
versions, so CI does not regenerate: it checks the committed files against
the SHA-256 in the labels.
"""

from __future__ import annotations

import hashlib
import json
import random
import string
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mimi.proof.banks import CORAL_LANE_ACCOUNT, bank_for  # noqa: E402
from mimi.proof.checks import match_invoice  # noqa: E402
from mimi.proof.flow import world_book  # noqa: E402
from mimi.proof.parse import LAGOS, norm_name  # noqa: E402
from mimi.proof.render import ScreenSpec, render, render_not_a_payment  # noqa: E402
from mimi.world import load_world  # noqa: E402

OUT = ROOT / "eval" / "proof"
SEED = 2610


def digits(rng: random.Random, n: int) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(n))


def reference_for(bank: str, rng: random.Random) -> str:
    if bank == "Kestrel Bank":
        return "KST" + digits(rng, 12)
    if bank == "Harmattan MFB":
        return "HMF-" + "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(10))
    if bank == "Lagoonpay":
        return "999" + digits(rng, 27)
    return f"{bank_for(bank).short}/TRF/" + digits(rng, 9)


def sender_as(bank: str, owner: str) -> str:
    return owner if bank == "Harmattan MFB" else owner.upper()


def main() -> int:
    world = load_world()
    rng = random.Random(SEED)
    live = {d.shop_id: d for d in world.live_deliveries}
    transfer = [d.shop_id for d in world.live_deliveries if world.shop(d.shop_id).bank]
    book = world_book(world, lambda: {d.delivery_id for d in world.live_deliveries})
    day = world.date_of(world.live_day)

    def delivered(shop_id: str) -> datetime:
        d = live[shop_id]
        return datetime(day.year, day.month, day.day, d.minute // 60, d.minute % 60, tzinfo=LAGOS)

    cases: list[dict[str, Any]] = []
    (OUT / "screens").mkdir(parents=True, exist_ok=True)

    def base(shop_id: str, bank: str | None = None, hours: float | None = None):
        shop = world.shop(shop_id)
        fb = bank_for(shop.bank) if bank is None else fictional_bank_by(bank)
        received = delivered(shop_id) + timedelta(hours=hours if hours is not None else rng.uniform(1.5, 7))
        received = received.replace(second=rng.randint(0, 59), microsecond=0)
        paid = received - timedelta(minutes=rng.randint(2, 40), seconds=rng.randint(0, 59))
        invoices = book(shop_id, received)
        spec = ScreenSpec(
            bank=fb.name,
            amount_kobo=live[shop_id].total_kobo,
            paid_at=paid,
            sender=sender_as(fb.name, shop.owner),
            reference=reference_for(fb.name, rng),
            session_id=("999" + digits(rng, 27)) if fb.name == "Kestrel Bank" else "",
            narration=rng.choice(["", "goods", f"{live[shop_id].delivery_id}", "payment", "CLT"]),
            status_bar_time=received.strftime("%H:%M"),
            seed=rng.randint(0, 10**6),
        )
        return shop, spec, received, invoices

    def fictional_bank_by(name: str):
        from mimi.proof.banks import bank_named

        b = bank_named(name)
        assert b is not None
        return b

    def add(
        cid: str,
        category: str,
        shop_id: str,
        spec: ScreenSpec | None,
        received: datetime,
        invoices: list,
        *,
        expected: str,
        fake_kind: str = "",
        rules: tuple[str, ...] = (),
        prior: list[dict[str, Any]] | None = None,
        note: str = "",
        image: tuple[bytes, dict[str, Any]] | None = None,
    ) -> None:
        if image is None:
            assert spec is not None
            data, truth = render(spec)
        else:
            data, truth = image
        path = OUT / "screens" / f"{cid}.jpg"
        path.write_bytes(data)
        exp_fields: dict[str, Any] = {}
        exp_match: dict[str, Any] | None = None
        if spec is not None and truth.get("is_payment_screenshot") and not spec.hide_amount:
            exp_fields = {
                "amount_kobo": spec.amount_kobo,
                "paid_at": spec.paid_at.strftime("%Y-%m-%dT%H:%M") if not spec.time_text else None,
                "sender": norm_name(truth.get("sender_name")) or None,
                "bank": spec.bank,
            }
            if expected == "likely_genuine":
                m = match_invoice(spec.amount_kobo, invoices)
                exp_match = {"kind": m.kind, "delivery_ids": list(m.delivery_ids)}
        cases.append(
            {
                "id": cid,
                "category": category,
                "fake_kind": fake_kind,
                "file": f"screens/{cid}.jpg",
                # Provenance (RAI-261): every screen is drawn by code; real ones would be
                # "real-with-consent", redacted, and reported separately.
                "source": "synthetic",
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "shop_id": shop_id,
                "sender_phone": world.shop(shop_id).phone,
                "received_at": received.isoformat(),
                "invoices": [
                    {
                        "delivery_id": i.delivery_id,
                        "delivered_at": i.delivered_at.isoformat(),
                        "outstanding_kobo": i.outstanding_kobo,
                        "rep_id": i.rep_id,
                    }
                    for i in invoices
                ],
                "prior": prior or [],
                "expected": expected,
                "expected_rules": list(rules),
                "expected_fields": exp_fields,
                "expected_match": exp_match,
                "degradation": (
                    {
                        k: getattr(spec, k)
                        for k in ("jpeg_quality", "rotate_deg", "blur", "crop_top", "crop_bottom", "scale")
                        if spec is not None and getattr(spec, k) not in (0, 0.0, 1.0, 82)
                    }
                    if spec is not None
                    else {}
                ),
                "truth": truth,
                "note": note,
            }
        )

    shops = list(transfer)
    rng.shuffle(shops)
    pick = iter(shops * 2)

    # ---------------------------------------------------------- genuine (20)
    looks = [
        {},
        {"rotate_deg": 2.5},
        {"blur": 0.9},
        {"crop_top": 44},
        {"jpeg_quality": 48},
        {"scale": 0.8},
        {"rotate_deg": -3.0, "jpeg_quality": 60},
        {"crop_top": 44, "crop_bottom": 80},
        {"blur": 1.1, "scale": 0.85},
        {},
    ]
    banks_cycle = ["Kestrel Bank", "Harmattan MFB", "Lagoonpay", "Ironleaf Bank"]
    for n in range(20):
        shop_id = next(pick)
        bank = banks_cycle[n % 4]
        shop, spec, received, invoices = base(shop_id, bank)
        kw: dict[str, Any] = dict(looks[n % len(looks)])
        if bank == "Kestrel Bank":
            kw["fee_kobo"] = rng.choice([1000, 2500, 5375])
        elif bank == "Harmattan MFB":
            kw["fee_kobo"] = rng.choice([0, 1000])
        elif bank == "Lagoonpay" and n % 8 == 2:
            kw["fee_kobo"] = 0
        note = "pays today's delivery"
        if n in (5, 13):
            open_ = sorted((i for i in invoices if i.outstanding_kobo > 0), key=lambda i: i.delivered_at)
            kw["amount_kobo"] = (open_[0].outstanding_kobo // 2) // 100_00 * 100_00 or 500_000
            note = "part payment"
        elif n == 9:
            kw["amount_kobo"] = sum(i.outstanding_kobo for i in invoices)
            note = "clears the whole open balance"
        elif n in (3, 17):
            older = [i for i in invoices if i.delivery_id != live[shop_id].delivery_id]
            if older:
                kw["amount_kobo"] = older[0].outstanding_kobo
                note = "pays an older open invoice"
        spec = replace(spec, **kw)
        add(f"g{n + 1:02d}", "genuine", shop_id, spec, received, invoices,
            expected="likely_genuine", note=note)

    # ------------------------------------------------------------ fakes (16)
    def fake(cid: str, kind: str, rules: tuple[str, ...], bank: str, mutate, *, prior=None,
             hours: float | None = None, note: str = "") -> None:
        shop_id = next(pick)
        shop, spec, received, invoices = base(shop_id, bank, hours)
        spec, received, extra_prior = mutate(spec, received, shop_id, invoices)
        add(cid, "fake", shop_id, spec, received, invoices, expected="looks_fake",
            fake_kind=kind, rules=rules, prior=(prior or []) + extra_prior, note=note)

    def edited(spec, received, shop_id, invoices):
        true_paid = spec.amount_kobo // 10
        fee = 1000 if spec.bank == "Kestrel Bank" else 0
        return (
            replace(spec, fee_kobo=fee, total_kobo=true_paid + fee, font_overrides={"amount": "serif_bold"},
                    amount_patch=True,
                    editing_signs=("amount in a different font on a lighter patch",)),
            received,
            [],
        )

    fake("f01", "edited_amount", ("arithmetic", "editing_signs"), "Kestrel Bank", edited,
         note="paid a tenth; big amount typed over, total debited left as it was")
    fake("f02", "edited_amount", ("arithmetic", "editing_signs"), "Harmattan MFB", edited,
         note="paid a tenth; amount typed over, total left as it was")

    def edited_sms(spec, received, shop_id, invoices):
        return (
            replace(spec, font_overrides={"amount": "serif"}, amount_patch=True,
                    editing_signs=("Amt line in a different font on a lighter patch",)),
            received,
            [],
        )

    fake("f03", "edited_amount", ("editing_signs",), "Ironleaf Bank", edited_sms,
         note="SMS amount line typed over in another font; nothing else to check against")

    def wrong_acct(spec, received, shop_id, invoices):
        return replace(spec, recipient_account="9990009917"), received, []

    fake("f04", "wrong_recipient", ("recipient",), "Kestrel Bank", wrong_acct,
         note="right name, someone else's account number")

    def wrong_name(spec, received, shop_id, invoices):
        return (
            replace(spec, recipient_name="CORAL LANE TRADERS VENTURES",
                    recipient_account="9990005512", recipient_bank="Harmattan MFB"),
            received,
            [],
        )

    fake("f05", "wrong_recipient", ("recipient",), "Lagoonpay", wrong_name,
         note="a look-alike name at another bank")

    def dup_ref(spec, received, shop_id, invoices):
        other = next(s for s in transfer if s != shop_id)
        prior = [{
            "proof_id": f"wamid.prior-{other}-1",
            "shop_id": other,
            "received_at": (received - timedelta(hours=3)).isoformat(),
            "image_sha256": hashlib.sha256(spec.reference.encode()).hexdigest(),
            "reference": spec.reference.replace(" ", "").upper(),
            "session_id": spec.session_id,
            "amount_kobo": spec.amount_kobo,
            "verdict": "likely_genuine",
            "level": "likely",
        }]
        return spec, received, prior

    fake("f06", "duplicated_reference", ("reference_unique",), "Harmattan MFB", dup_ref,
         note="another shop sent a receipt with this reference three hours earlier")
    fake("f07", "duplicated_reference", ("reference_unique",), "Ironleaf Bank", dup_ref,
         note="another shop sent this reference three hours earlier")

    def bad_date(spec, received, shop_id, invoices):
        return replace(spec, time_text="Sep 31, 2026 02:15:44 PM"), received, []

    fake("f08", "impossible_time", ("time_real",), "Harmattan MFB", bad_date,
         note="31 September does not exist")

    def future(spec, received, shop_id, invoices):
        return replace(spec, paid_at=received + timedelta(hours=4, minutes=13)), received, []

    fake("f09", "impossible_time", ("time_not_future",), "Kestrel Bank", future,
         note="paid four hours after it was sent")

    def bad_hour(spec, received, shop_id, invoices):
        return replace(spec, time_text=f"{received:%d/%m/%Y} 25:14"), received, []

    fake("f10", "impossible_time", ("time_real",), "Ironleaf Bank", bad_hour,
         note="25:14 is not a time")

    def pending(spec, received, shop_id, invoices):
        return replace(spec, headline="Transfer Successful", status_detail="Pending"), received, []

    fake("f11", "pending_as_success", ("status",), "Kestrel Bank", pending,
         note="big success tick, but the status row says Pending")

    def processing(spec, received, shop_id, invoices):
        return replace(spec, status_detail="Processing"), received, []

    fake("f12", "pending_as_success", ("status",), "Lagoonpay", processing,
         note="'Payment sent' headline over a Processing status")

    def fonts_a(spec, received, shop_id, invoices):
        return (
            replace(spec, font_overrides={"sender": "serif", "amount": "serif_bold"},
                    session_id=spec.session_id[:18],
                    editing_signs=("amount and sender in a different font",)),
            received,
            [],
        )

    fake("f13", "mismatched_fonts", ("editing_signs", "reference_format"), "Kestrel Bank", fonts_a,
         note="amount and sender in a serif font; session id cut to 18 digits")

    def fonts_b(spec, received, shop_id, invoices):
        return (
            replace(spec, font_overrides={"time": "serif", "reference": "mono", "amount": "serif_bold"},
                    editing_signs=("date, reference and amount in different fonts",)),
            received,
            [],
        )

    fake("f14", "mismatched_fonts", ("editing_signs",), "Harmattan MFB", fonts_b,
         note="date, reference and amount each in a different font")

    def old_reused(spec, received, shop_id, invoices):
        then = received - timedelta(days=35)
        prior = [{
            "proof_id": f"wamid.prior-{shop_id}-old",
            "shop_id": shop_id,
            "received_at": (then + timedelta(minutes=9)).isoformat(),
            "image_sha256": hashlib.sha256((spec.reference + "old").encode()).hexdigest(),
            "reference": spec.reference.upper(),
            "session_id": spec.session_id,
            "amount_kobo": spec.amount_kobo,
            "verdict": "likely_genuine",
            "level": "verified",
        }]
        return replace(spec, paid_at=then, status_bar_time=received.strftime("%H:%M")), received, prior

    fake("f15", "old_screenshot", ("time_recent", "reference_unique", "time_after_delivery"),
         "Lagoonpay", old_reused, note="last month's receipt from this shop, sent again")

    def old_plain(spec, received, shop_id, invoices):
        return replace(spec, paid_at=received - timedelta(days=21)), received, []

    fake("f16", "old_screenshot", ("time_recent", "time_after_delivery"), "Ironleaf Bank", old_plain,
         note="a three-week-old alert")

    # ------------------------------------------------------- unreadable (4)
    shop_id = next(pick)
    _, spec, received, invoices = base(shop_id, "Kestrel Bank")
    add("u01", "unreadable", shop_id, replace(spec, blur=5.0, scale=0.6, legibility=0.15,
        amount_confidence=0.2), received, invoices, expected="cannot_tell",
        note="far too blurred to read")
    shop_id = next(pick)
    _, spec, received, invoices = base(shop_id, "Harmattan MFB")
    add("u02", "unreadable", shop_id, replace(spec, hide_amount=True, crop_top=150), received,
        invoices, expected="cannot_tell", note="cropped: the amount is cut off")
    shop_id = next(pick)
    _, spec, received, invoices = base(shop_id, "Kestrel Bank")
    add("u03", "unreadable", shop_id, None, received, invoices, expected="cannot_tell",
        note="a photo of a shelf, not a payment", image=render_not_a_payment(SEED))
    shop_id = next(pick)
    _, spec, received, invoices = base(shop_id, "Lagoonpay")
    add("u04", "unreadable", shop_id, replace(spec, scale=0.22, jpeg_quality=25, legibility=0.2,
        amount_confidence=0.3), received, invoices, expected="cannot_tell",
        note="a tiny, heavily compressed thumbnail")

    with (OUT / "screens.jsonl").open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"wrote {len(cases)} cases to {OUT}")
    assert len(cases) == 40
    assert CORAL_LANE_ACCOUNT.last4 == "4471"
    return 0


if __name__ == "__main__":
    sys.exit(main())
