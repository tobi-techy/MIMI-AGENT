"""Code checks on a payment screenshot, the verdict, and the invoice match.

``assess`` takes what Muse Spark copied off the screenshot (``ProofReading``),
the shop's open invoices (the book) and the proofs Mimi has already seen, and
decides, in code:

* ``cannot_tell``: not a payment screenshot, or the amount, time or recipient
  cannot be read with certainty. Mimi asks the shop once for a clearer one.
* ``looks_fake``: a hard check failed (status not successful, wrong
  recipient, impossible / future / too old / before-delivery time, bad
  reference format, a reference or image already used, figures that do not
  add up, or editing signs the model pointed at). It goes to the owner's
  approvals with every failed rule named. Mimi never accuses the shop.
* ``likely_genuine``: every hard check passed. The payment is *likely*, never
  verified: only the bank's own credit (``witness.py``) makes it verified.

The amount is matched to an open invoice (exact, the whole open balance, a
part payment on the oldest, or more than is owed). Nothing here moves money
or changes credit; every figure is integer kobo parsed in code.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol

from mimi.proof.banks import CORAL_LANE_ACCOUNT, CollectionAccount, FictionalBank, bank_named
from mimi.proof.parse import (
    amount_kobo,
    last_digits,
    norm_name,
    norm_ref,
    parse_time,
)
from mimi.proof.reading import ProofReading

Verdict = Literal["likely_genuine", "looks_fake", "cannot_tell"]
VERDICTS: tuple[str, ...] = ("likely_genuine", "looks_fake", "cannot_tell")
CheckResult = Literal["pass", "fail", "warn", "unknown"]
MatchKind = Literal["exact", "all_open", "partial", "over", "none"]

MAX_AGE = timedelta(days=14)
# Phone clocks drift; a receipt a few minutes "ahead" of the message is fine.
FUTURE_SLACK = timedelta(minutes=5)
MIN_LEGIBILITY = 0.5
MIN_AMOUNT_CONFIDENCE = 0.8
MIN_EDIT_CONFIDENCE = 0.6

_BAD_STATUS = re.compile(
    r"\b(pending|processing|in progress|awaiting|queued|failed|declined|reversed|"
    r"unsuccessful|not successful|cancell?ed|on hold)\b",
    re.I,
)
_GOOD_STATUS = re.compile(r"\b(successful|success|completed|approved|sent|debit|dr)\b", re.I)

# Rules whose failure makes the screenshot look fake.
HARD_RULES: tuple[str, ...] = (
    "status",
    "recipient",
    "time_real",
    "time_not_future",
    "time_recent",
    "time_after_delivery",
    "reference_format",
    "reference_unique",
    "same_image",
    "arithmetic",
    "editing_signs",
)


@dataclass(frozen=True)
class OpenInvoice:
    """One open receivable on the shop's book (a delivery not yet paid)."""

    delivery_id: str
    delivered_at: datetime
    outstanding_kobo: int
    rep_id: str = ""


@dataclass(frozen=True)
class PriorProof:
    proof_id: str
    shop_id: str
    image_sha256: str
    reference: str
    session_id: str
    received_at: str
    amount_kobo: int | None
    verdict: str
    delivery_ids: tuple[str, ...] = ()


class ProofHistory(Protocol):
    """What Mimi already saw (any shop of this distributor)."""

    def by_image(self, sha256: str) -> list[PriorProof]: ...

    def by_reference(self, ref: str) -> list[PriorProof]: ...


@dataclass(frozen=True)
class Check:
    name: str
    result: CheckResult
    detail: str = ""


@dataclass(frozen=True)
class Match:
    kind: MatchKind
    delivery_ids: tuple[str, ...] = ()
    open_kobo: int = 0


@dataclass(frozen=True)
class Assessment:
    verdict: Verdict
    checks: tuple[Check, ...]
    amount_kobo: int | None = None
    paid_at: datetime | None = None
    bank: str = ""
    sender: str = ""
    reference: str = ""
    session_id: str = ""
    match: Match = field(default_factory=lambda: Match("none"))
    # Same image from the same shop already seen: nothing new is matched.
    duplicate_of: str = ""

    @property
    def failed(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.result == "fail")

    @property
    def reasons(self) -> tuple[str, ...]:
        """Plain reasons for the owner: each failed (or blocking) rule named."""
        bad = [c for c in self.checks if c.result == "fail"]
        if self.verdict == "cannot_tell":
            bad = [c for c in self.checks if c.result in ("fail", "unknown")]
        return tuple(f"{c.name}: {c.detail}" for c in bad)


def _naira(kobo: int) -> str:
    whole, part = divmod(kobo, 100)
    return f"N{whole:,}" + (f".{part:02d}" if part else "")


def match_invoice(amount: int, invoices: Sequence[OpenInvoice]) -> Match:
    """Exact invoice first (oldest when several), then the whole open
    balance, then a part payment on the oldest, then over-payment."""
    open_ = sorted((i for i in invoices if i.outstanding_kobo > 0), key=lambda i: i.delivered_at)
    total = sum(i.outstanding_kobo for i in open_)
    if not open_:
        return Match("none")
    for inv in open_:
        if inv.outstanding_kobo == amount:
            return Match("exact", (inv.delivery_id,), inv.outstanding_kobo)
    if amount == total:
        return Match("all_open", tuple(i.delivery_id for i in open_), total)
    if amount < total:
        return Match("partial", (open_[0].delivery_id,), open_[0].outstanding_kobo)
    return Match("over", tuple(i.delivery_id for i in open_), total)


def _readable(r: ProofReading, amount: int | None, time_status: str) -> Check:
    if not r.is_payment_screenshot:
        return Check("readable", "unknown", "not a bank transfer screenshot")
    if r.legibility < MIN_LEGIBILITY:
        return Check("readable", "unknown", f"too blurred to read (legibility {r.legibility:.2f})")
    if amount is None:
        shown = r.amount_as_printed or "not shown"
        return Check("readable", "unknown", f"amount could not be read ({shown})")
    if r.amount_confidence < MIN_AMOUNT_CONFIDENCE:
        return Check(
            "readable", "unknown", f"amount not read with certainty ({r.amount_confidence:.2f})"
        )
    if time_status == "missing":
        return Check("readable", "unknown", "date and time not shown")
    return Check("readable", "pass", "amount and time read")


def _status(r: ProofReading) -> Check:
    words = " | ".join(s for s in (r.headline_status, r.status_detail) if s)
    if _BAD_STATUS.search(words):
        return Check("status", "fail", f"status says {words!r}, not a completed transfer")
    if _GOOD_STATUS.search(words) or (r.layout == "sms_alert" and not words):
        return Check("status", "pass", words or "debit alert")
    return Check("status", "warn", f"no clear success status ({words or 'none shown'})")


def _recipient(r: ProofReading, account: CollectionAccount) -> Check:
    name = norm_name(r.recipient_name) or norm_name(r.narration if r.layout == "sms_alert" else "")
    digits = last_digits(r.recipient_account_as_printed)
    rbank = bank_named(r.recipient_bank) if r.recipient_bank else None
    if not name and not digits:
        return Check("recipient", "unknown", "recipient not shown")
    if name and norm_name(account.name) not in name:
        return Check("recipient", "fail", f"paid to {r.recipient_name!r}, not {account.name}")
    if digits and digits != account.last4:
        return Check(
            "recipient",
            "fail",
            f"paid to account ending {digits}, {account.name} ends {account.last4}",
        )
    if r.recipient_bank and (rbank is None or rbank.name != account.bank):
        return Check("recipient", "fail", f"paid into {r.recipient_bank!r}, not {account.bank}")
    return Check("recipient", "pass", f"{account.name} ending {account.last4}")


def _reference(
    r: ProofReading, bank: FictionalBank | None
) -> tuple[Check, str, str]:
    ref = norm_ref(r.reference)
    sess = norm_ref(r.session_id)
    if bank is None:
        return Check("reference_format", "warn", "bank not recognised; format not checked"), ref, sess
    if not ref:
        return Check("reference_format", "warn", "no reference shown"), ref, sess
    if not bank.reference_ok(ref):
        if _near_session_id(ref) and bank.reference_ok("999" + "0" * 27):
            return (
                Check("reference_format", "warn", f"{ref!r} is a digit or two off; may be misread"),
                ref,
                sess,
            )
        return (
            Check("reference_format", "fail", f"{ref!r} is not a {bank.name} reference"),
            ref,
            sess,
        )
    if sess and not re.fullmatch(r"999\d{27}", sess):
        if _near_session_id(sess):
            return (
                Check("reference_format", "warn", f"session id {sess!r} may be misread"),
                ref,
                sess,
            )
        return Check("reference_format", "fail", f"session id {sess!r} is malformed"), ref, sess
    return Check("reference_format", "pass", ref), ref, sess


def _near_session_id(value: str) -> bool:
    """A 30-digit session id read with a digit or two dropped or doubled.

    Long digit runs are where reading slips (RAI-269, g11: 29 digits read
    off a genuine 30-digit id). That is not evidence of forgery, so it is a
    warning; a session id far from 30 digits (an 18-digit one) still fails.
    """
    return value.isdigit() and value.startswith("999") and 28 <= len(value) <= 32


def _arithmetic(r: ProofReading, amount: int) -> Check:
    if not r.total_as_printed:
        return Check("arithmetic", "pass", "no total printed")
    total = amount_kobo(r.total_as_printed)
    fee = amount_kobo(r.fee_as_printed) if r.fee_as_printed else 0
    if total is None or (r.fee_as_printed and fee is None):
        return Check("arithmetic", "warn", "fee or total could not be read")
    assert fee is not None
    if amount + fee != total:
        return Check(
            "arithmetic",
            "fail",
            f"amount {_naira(amount)} + fee {_naira(fee)} is not the total {_naira(total)}",
        )
    return Check("arithmetic", "pass", f"{_naira(amount)} + {_naira(fee)} = {_naira(total)}")


def assess(
    reading: ProofReading,
    *,
    shop_id: str,
    proof_id: str,
    image_sha256: str,
    received_at: datetime,
    invoices: Sequence[OpenInvoice],
    history: ProofHistory,
    account: CollectionAccount = CORAL_LANE_ACCOUNT,
) -> Assessment:
    r = reading
    bank = bank_named(r.bank_name)
    amount = amount_kobo(r.amount_as_printed)
    tr = parse_time(r.date_time_as_printed, bank)
    checks: list[Check] = [_readable(r, amount, tr.status)]

    # The same image from the same shop: a resend, not a second payment.
    same_img = [p for p in history.by_image(image_sha256) if p.proof_id != proof_id]
    for p in same_img:
        if p.shop_id == shop_id:
            checks.append(Check("same_image", "warn", f"already received as {p.proof_id}"))
            return Assessment(
                verdict=p.verdict if p.verdict in VERDICTS else "cannot_tell",  # type: ignore[arg-type]
                checks=tuple(checks),
                amount_kobo=p.amount_kobo,
                reference=p.reference,
                duplicate_of=p.proof_id,
            )
    if checks[0].result != "pass":
        return Assessment("cannot_tell", tuple(checks), amount_kobo=amount)
    assert amount is not None
    if same_img:
        checks.append(Check("same_image", "fail", "the same image was sent by another shop"))
    else:
        checks.append(Check("same_image", "pass", "first time seen"))
    checks.append(_status(r))
    rec = _recipient(r, account)
    checks.append(rec)

    paid_at = tr.at
    open_ = [i for i in invoices if i.outstanding_kobo > 0]
    match = match_invoice(amount, open_)
    if tr.status == "impossible" or paid_at is None:
        checks.append(
            Check("time_real", "fail", f"{r.date_time_as_printed!r} is not a real date and time")
        )
    else:
        checks.append(Check("time_real", "pass", f"{paid_at:%Y-%m-%d %H:%M} WAT"))
        if paid_at > received_at + FUTURE_SLACK:
            checks.append(
                Check(
                    "time_not_future",
                    "fail",
                    f"paid at {paid_at:%d %b %H:%M}, after it was sent ({received_at:%d %b %H:%M})",
                )
            )
        else:
            checks.append(Check("time_not_future", "pass", "before it was sent"))
        if received_at - paid_at > MAX_AGE:
            checks.append(
                Check(
                    "time_recent",
                    "fail",
                    f"paid {(received_at - paid_at).days} days before it was sent "
                    f"(limit {MAX_AGE.days})",
                )
            )
        else:
            checks.append(Check("time_recent", "pass", f"within {MAX_AGE.days} days"))
        if open_:
            if match.kind == "exact":
                due_from = next(i for i in open_ if i.delivery_id == match.delivery_ids[0])
            else:
                due_from = min(open_, key=lambda i: i.delivered_at)
            if paid_at < due_from.delivered_at:
                checks.append(
                    Check(
                        "time_after_delivery",
                        "fail",
                        f"paid {paid_at:%d %b %H:%M}, before delivery {due_from.delivery_id} "
                        f"({due_from.delivered_at:%d %b %H:%M})",
                    )
                )
            else:
                checks.append(Check("time_after_delivery", "pass", "after the delivery"))

    ref_check, ref, sess = _reference(r, bank)
    checks.append(ref_check)
    reused = [
        p
        for key in {k for k in (ref, sess) if k}
        for p in history.by_reference(key)
        if p.proof_id != proof_id
    ]
    if reused:
        p = reused[0]
        who = "this shop" if p.shop_id == shop_id else "another shop"
        checks.append(
            Check(
                "reference_unique",
                "fail",
                f"reference already used by {who} ({p.proof_id}, {p.received_at[:10]})",
            )
        )
    elif ref or sess:
        checks.append(Check("reference_unique", "pass", "not seen before"))
    checks.append(_arithmetic(r, amount))
    if r.editing_signs and r.editing_confidence >= MIN_EDIT_CONFIDENCE:
        checks.append(
            Check("editing_signs", "fail", "model saw: " + "; ".join(r.editing_signs)[:240])
        )
    elif odd := r.typefaces.odd_ones():
        checks.append(
            Check(
                "editing_signs",
                "fail",
                f"{', '.join(odd)} printed in a different typeface from the rest "
                f"({r.typefaces.other_rows})",
            )
        )
    else:
        checks.append(Check("editing_signs", "pass", "none seen"))
    checks.append(
        Check("bank_known", "pass" if bank else "warn", bank.name if bank else str(r.bank_name))
    )
    checks.append(
        Check(
            "amount_vs_book",
            "pass" if match.kind in ("exact", "all_open") else "warn",
            {
                "exact": "matches an open invoice",
                "all_open": "matches the whole open balance",
                "partial": f"part payment; {_naira(match.open_kobo)} open on the oldest invoice",
                "over": f"more than the {_naira(match.open_kobo)} open",
                "none": "no open invoice",
            }[match.kind],
        )
    )

    hard_fail = any(c.result == "fail" and c.name in HARD_RULES for c in checks)
    verdict: Verdict
    if hard_fail:
        verdict = "looks_fake"
    elif rec.result == "unknown":
        verdict = "cannot_tell"
    else:
        verdict = "likely_genuine"
    return Assessment(
        verdict=verdict,
        checks=tuple(checks),
        amount_kobo=amount,
        paid_at=paid_at,
        bank=bank.name if bank else (r.bank_name or ""),
        sender=r.sender_name or "",
        reference=ref,
        session_id=sess,
        match=match if verdict == "likely_genuine" else Match("none"),
    )
