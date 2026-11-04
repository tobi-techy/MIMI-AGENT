"""Credit exposure per shop and in total (feat/returns-and-exposure): code, no model.

What one shop could cost the distributor, from the book's open receivables,
aged and risk-weighted, so a record becomes a decision.

Rules, all in code and versioned (``EXPOSURE_POLICY_VERSION``):

* **The primary figures are ``outstanding_kobo`` and ``overdue_kobo``**: money
  the shop actually owes, from the book. ``exposure_kobo`` is a second,
  clearly labelled figure: the same money weighted up by how old it is.
* **Ageing bands** (``BUCKETS``), from ``days_overdue`` (the one definition of
  overdue, ``mimi.owed``): ``current`` 0 days, ``days_1_30`` 1 to 30,
  ``days_31_60`` 31 to 60, ``days_over_60`` 61 or more. The weights (100, 125,
  150, 200 percent) are an assumption, not money anyone owes, so they are
  always shown beside the figure.
* **Only bank-verified payments count** (the rule ``mimi.credit.review``
  already follows): a payment the shop reported, with no bank credit, never
  lowers exposure; it is reported as ``shown_not_counted_kobo``.
* **Confidence**, defined here, never by feel. It measures how much the bank
  feed backs the reading, from the count of bank-verified payments in the last
  ``WINDOW_DAYS`` (8 weeks):
  * ``high``: 8 or more verified payments;
  * ``medium``: 4 to 7 (``MIN_VERIFIED`` or more);
  * ``low``: fewer than 4, including a shop with no verified history.
  The total's confidence is the weakest shop's, because one unbacked shop in a
  healthy book is the one that can surprise the owner.

Nothing here reads a database, calls a model or moves money.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from mimi import owed
from mimi.credit.review import MIN_VERIFIED, WINDOW_DAYS
from mimi.provenance import verified_records

EXPOSURE_POLICY_VERSION = "exposure-v0.1"
STRONG_VERIFIED = 8


@dataclass(frozen=True)
class Bucket:
    key: str
    label: str
    from_days: int | None
    to_days: int | None
    weight_pct: int


BUCKETS: tuple[Bucket, ...] = (
    Bucket("current", "Not yet due", None, 0, 100),
    Bucket("days_1_30", "1 to 30 days", 1, 30, 125),
    Bucket("days_31_60", "31 to 60 days", 31, 60, 150),
    Bucket("days_over_60", "Over 60 days", 61, None, 200),
)
_BY_KEY = {b.key: b for b in BUCKETS}


def buckets_definition() -> list[dict[str, Any]]:
    """The bands and weights, so the console never keeps its own copy."""
    return [asdict(b) for b in BUCKETS]


def bucket_of(days_overdue: int) -> str:
    days = max(0, int(days_overdue))
    for b in BUCKETS:
        if b.from_days is None and b.to_days is not None and days <= b.to_days:
            return b.key
        if b.from_days is not None and days >= b.from_days and (
                b.to_days is None or days <= b.to_days):
            return b.key
    return BUCKETS[-1].key


def weight_pct(bucket: str) -> int:
    return _BY_KEY[bucket].weight_pct


def weighted_kobo(outstanding_kobo: int, days_overdue: int) -> int:
    """One receivable's risk-weighted exposure, in kobo, rounded to the naira."""
    raw = int(outstanding_kobo) * weight_pct(bucket_of(days_overdue))
    return int((raw + 50) // 100)


def confidence_for(verified_payments: int) -> str:
    """The defined grade: how much bank-verified history backs the figure."""
    if verified_payments >= STRONG_VERIFIED:
        return "high"
    return "medium" if verified_payments >= MIN_VERIFIED else "low"


@dataclass
class ShopExposure:
    shop_id: str
    shop_name: str
    outstanding_kobo: int = 0
    overdue_kobo: int = 0
    oldest_days_overdue: int = 0
    bucket_kobo: dict[str, int] = field(default_factory=dict)
    exposure_kobo: int = 0
    verified_payments: int = 0
    verified_paid_kobo: int = 0
    shown_not_counted_kobo: int = 0
    open_invoices: int = 0
    confidence: str = "low"

    def as_dict(self) -> dict[str, Any]:
        return {
            "shop_id": self.shop_id, "shop_name": self.shop_name,
            "outstanding_kobo": self.outstanding_kobo,
            "outstanding_naira": owed.naira(self.outstanding_kobo),
            "overdue_kobo": self.overdue_kobo,
            "overdue_naira": owed.naira(self.overdue_kobo),
            "oldest_days_overdue": self.oldest_days_overdue,
            "buckets": {f"{b.key}_kobo": self.bucket_kobo.get(b.key, 0) for b in BUCKETS},
            "exposure_kobo": self.exposure_kobo,
            "exposure_naira": owed.naira(self.exposure_kobo),
            "verified_paid_kobo": self.verified_paid_kobo,
            "shown_not_counted_kobo": self.shown_not_counted_kobo,
            "open_invoices": self.open_invoices,
            "confidence": self.confidence,
            "note": self.note(),
        }

    def note(self) -> str:
        """One plain sentence, no ids, no dashes; weights always visible."""
        where = (f"{owed.naira(self.overdue_kobo)} overdue, oldest "
                 f"{self.oldest_days_overdue} days" if self.overdue_kobo else "nothing overdue")
        weeks = WINDOW_DAYS // 7
        bank = (f"{self.verified_payments} bank-verified "
                f"{'payment' if self.verified_payments == 1 else 'payments'} in "
                f"{weeks} weeks" if self.verified_payments
                else f"no bank-verified payments in {weeks} weeks")
        shown = (f" {owed.naira(self.shown_not_counted_kobo)} reported is shown, not counted."
                 if self.shown_not_counted_kobo else "")
        return (f"{self.shop_name}: {owed.naira(self.outstanding_kobo)} outstanding, {where}. "
                f"Risk-weighted {owed.naira(self.exposure_kobo)} at the bands above (an "
                f"assumption, not money owed). {bank}, so {self.confidence} confidence."
                f"{shown}")


def shop_exposure(shop_id: str, shop_name: str,
                  receivables: Sequence[Mapping[str, Any]],
                  payments: Iterable[Mapping[str, Any]],
                  as_of_day: int, window_days: int = WINDOW_DAYS) -> ShopExposure:
    """One shop's exposure. ``payments`` are the shop's payment rows (each with
    ``day``, ``amount_kobo``, ``claimed_kobo`` and a proof level); only
    bank-verified ones inside the window count."""
    row = ShopExposure(shop_id=shop_id, shop_name=shop_name)
    for r in receivables:
        out = int(r.get("outstanding_kobo") or 0)
        if out <= 0:
            continue
        days = int(r.get("days_overdue") or 0)
        key = bucket_of(days)
        row.bucket_kobo[key] = row.bucket_kobo.get(key, 0) + out
        row.outstanding_kobo += out
        row.open_invoices += 1
        row.exposure_kobo += weighted_kobo(out, days)
        if days > 0:
            row.overdue_kobo += out
            row.oldest_days_overdue = max(row.oldest_days_overdue, days)
    in_window = [p for p in payments if int(p.get("day") or 0) >= as_of_day - window_days]
    counted = {id(p) for p in verified_records(in_window) if int(p.get("amount_kobo") or 0) > 0}
    row.verified_payments = len(counted)
    row.verified_paid_kobo = sum(int(p.get("amount_kobo") or 0) for p in in_window
                                 if id(p) in counted)
    row.shown_not_counted_kobo = sum(int(p.get("claimed_kobo") or 0) for p in in_window
                                     if id(p) not in counted)
    row.confidence = confidence_for(row.verified_payments)
    return row


def total_exposure(shops: Sequence[ShopExposure]) -> ShopExposure:
    """Every shop summed. Confidence is the weakest shop's with exposure."""
    row = ShopExposure(shop_id="", shop_name="All shops")
    with_exposure = [s for s in shops if s.outstanding_kobo > 0]
    for s in shops:
        row.outstanding_kobo += s.outstanding_kobo
        row.overdue_kobo += s.overdue_kobo
        row.exposure_kobo += s.exposure_kobo
        row.verified_payments += s.verified_payments
        row.verified_paid_kobo += s.verified_paid_kobo
        row.shown_not_counted_kobo += s.shown_not_counted_kobo
        row.open_invoices += s.open_invoices
        row.oldest_days_overdue = max(row.oldest_days_overdue, s.oldest_days_overdue)
        for k, v in s.bucket_kobo.items():
            row.bucket_kobo[k] = row.bucket_kobo.get(k, 0) + v
    row.confidence = (min((s.confidence for s in with_exposure),
                          key=lambda c: {"low": 0, "medium": 1, "high": 2}[c])
                      if with_exposure else "low")
    return row


def weights_note() -> str:
    bands = ", ".join(f"{b.label} {b.weight_pct} percent" for b in BUCKETS)
    return ("Exposure is the money owed, weighted up by how old it is "
            f"({bands}). The weights are an assumption, not money anyone owes; "
            "outstanding and overdue are the money owed.")


def exposure_summary(rows: Iterable[ShopExposure]) -> dict[str, Any]:
    """The whole response body the API serves."""
    shops = sorted(rows, key=lambda s: (-s.exposure_kobo, s.shop_id))
    return {
        "as_of": "",
        "policy_version": EXPOSURE_POLICY_VERSION,
        "buckets": buckets_definition(),
        "note": weights_note(),
        "shops": [s.as_dict() for s in shops],
        "total": total_exposure(shops).as_dict(),
    }
