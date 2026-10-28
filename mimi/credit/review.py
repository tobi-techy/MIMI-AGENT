"""The credit file and the recommendation: plain code, no model.

``credit_file(history, shop, window_days)`` turns the book's history into
the figures an owner looks at. Only bank-verified payments count (RAI-295):
a payment the shop reported, or a cash payment to the rep that the bank feed
does not show, is listed as "shown, not counted".

``recommend(file, shelf)`` gives one of three outcomes, with a confidence
and short reasons, all written by code:

* ``support``: verified on-time payer (at least ``MIN_VERIFIED`` verified
  payments, ``SUPPORT_ON_TIME`` percent or more on time), nothing overdue,
  at most one broken promise and no open dispute;
* ``not_now``: anything overdue past ``LATE_DAYS`` days, two or more broken
  promises, or a verified on-time share under ``POOR_ON_TIME`` percent;
* ``hold``: everything in between, including a shop whose payments cannot be
  verified yet (cash to the rep) and a shelf that shows more of an item than
  the distributor delivered in four weeks (stock may be borrowed or from
  elsewhere: RAI-271's gaming risk).

The shelf count is supporting evidence only: it can turn a ``support`` into
a ``hold`` and lower the confidence, never raise anything. It never suggests
a limit: the owner sets credit (RAI-271 non-goal "setting or changing
limits").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from mimi.credit.shelf import ShelfCount
from mimi.harness.loop import naira_text

RECOMMENDER_VERSION = "credit-review-v0.1"
Outcome = Literal["support", "hold", "not_now"]
Confidence = Literal["high", "medium", "low"]

WINDOW_DAYS = 56
MIN_VERIFIED = 4
SUPPORT_ON_TIME = 85
POOR_ON_TIME = 60
LATE_DAYS = 14
# Pieces seen over pieces delivered in four weeks before the shelf looks odd.
SHELF_SLACK = 1.5
SHELF_SLACK_PIECES = 6


@dataclass(frozen=True)
class CreditFile:
    shop_id: str
    shop_name: str
    current_limit_kobo: int
    outstanding_kobo: int
    overdue_kobo: int
    oldest_days_overdue: int
    window_days: int
    deliveries: int
    delivered_kobo: int
    verified_payments: int
    verified_paid_kobo: int
    verified_on_time: int
    on_time_pct: int
    unverified_payments: int
    unverified_kobo: int
    promises_made: int
    promises_broken: int
    open_disputes: int
    utilisation_pct: int
    # Pieces delivered per SKU in the last 28 days (for the shelf check).
    recent_pieces: Mapping[str, int] = field(default_factory=dict)
    candidates: tuple[str, ...] = ()


def credit_file(history: Mapping[str, Any], shop: Mapping[str, Any],
                receivables: Sequence[Mapping[str, Any]],
                prices: Mapping[str, tuple[int, int, str, str]],
                window_days: int = WINDOW_DAYS) -> CreditFile:
    live = int(history["live_day"])
    start = live - window_days
    dels = [d for d in history["deliveries"] if d["day"] >= start]
    due_by_delivery = {d["delivery_id"]: int(d["due_day"]) for d in history["deliveries"]}
    pays = [p for p in history["payments"] if p["day"] >= start]
    verified = [p for p in pays if p["bank_confirmed"] and p["amount_kobo"] > 0]
    unverified = [p for p in pays if not (p["bank_confirmed"] and p["amount_kobo"] > 0)]
    on_time = sum(1 for p in verified if p["day"] <= due_by_delivery.get(p["delivery_id"], p["day"]))
    proms = [p for p in history["promises"] if p["made_day"] >= start]
    overdue = [r for r in receivables if r["status"] == "overdue"]
    outstanding = sum(int(r["outstanding_kobo"]) for r in receivables)
    limit = int(shop.get("credit_limit_kobo") or 0)
    recent: dict[str, int] = {}
    for d in history["deliveries"]:
        if d["day"] >= live - 28:
            for ln in d["lines"]:
                pieces = prices.get(ln["sku"], (0, 1, "", ""))[1]
                recent[ln["sku"]] = recent.get(ln["sku"], 0) + int(ln["quantity"]) * pieces
    seen: list[str] = []
    for d in dels:
        for ln in d["lines"]:
            if ln["sku"] not in seen:
                seen.append(ln["sku"])
    return CreditFile(
        shop_id=str(shop["shop_id"]), shop_name=str(shop["name"]),
        current_limit_kobo=limit, outstanding_kobo=outstanding,
        overdue_kobo=sum(int(r["outstanding_kobo"]) for r in overdue),
        oldest_days_overdue=max((int(r["days_overdue"]) for r in overdue), default=0),
        window_days=window_days, deliveries=len(dels),
        delivered_kobo=sum(int(d["total_kobo"]) for d in dels),
        verified_payments=len(verified),
        verified_paid_kobo=sum(int(p["amount_kobo"]) for p in verified),
        verified_on_time=on_time,
        on_time_pct=(100 * on_time // len(verified)) if verified else 0,
        unverified_payments=len(unverified),
        unverified_kobo=sum(int(p["claimed_kobo"]) for p in unverified),
        promises_made=len(proms), promises_broken=sum(1 for p in proms if not p["kept"]),
        open_disputes=int(history.get("open_disputes") or 0),
        utilisation_pct=(100 * outstanding // limit) if limit else 0,
        recent_pieces=recent, candidates=tuple(seen),
    )


@dataclass(frozen=True)
class Recommendation:
    outcome: Outcome
    confidence: Confidence
    reasons: tuple[str, ...]
    flags: tuple[str, ...]
    version: str = RECOMMENDER_VERSION


def shelf_flags(f: CreditFile, shelf: ShelfCount | None) -> list[str]:
    if shelf is None or not shelf.usable:
        return []
    flags = []
    for item in shelf.items:
        delivered = f.recent_pieces.get(item.sku, 0)
        if item.about > SHELF_SLACK * delivered + SHELF_SLACK_PIECES:
            flags.append(f"shelf_more_than_delivered:{item.sku}")
    return flags


def _shelf_line(shelf: ShelfCount, about_kobo: int) -> str:
    parts = [f"about {i.about} {i.name} {i.pieces_word if i.about != 1 else i.piece}"
             for i in shelf.items[:3]]
    more = len(shelf.items) - 3
    listed = ", ".join(parts) + (
        f" and {more} more {'item' if more == 1 else 'items'}" if more > 0 else "")
    return (f"Shelf photo: {listed}, worth about {naira_text(about_kobo)} at the "
            f"distributor's prices ({shelf.confidence} confidence; supporting evidence only).")


def about_kobo(kobo: int) -> int:
    """'About' a shelf value: to the nearest N100, in kobo."""
    return ((kobo + 5_000) // 10_000) * 10_000


def recommend(f: CreditFile, shelf: ShelfCount | None) -> Recommendation:
    reasons: list[str] = []
    flags: list[str] = []
    days = f.window_days // 7
    if f.verified_payments:
        reasons.append(f"{f.verified_on_time} of {f.verified_payments} bank-verified payments "
                       f"on time in {days} weeks ({naira_text(f.verified_paid_kobo)} verified).")
    else:
        reasons.append(f"No bank-verified payments in {days} weeks to count.")
    if f.unverified_payments:
        reasons.append(f"{f.unverified_payments} payments not in the bank feed "
                       f"({naira_text(f.unverified_kobo)}): shown, not counted.")
    if f.overdue_kobo:
        reasons.append(f"{naira_text(f.overdue_kobo)} overdue, oldest {f.oldest_days_overdue} "
                       "days.")
    else:
        reasons.append("Nothing overdue.")
    if f.promises_broken:
        reasons.append(f"{f.promises_broken} of {f.promises_made} payment promises broken.")
    if f.open_disputes:
        reasons.append(f"{f.open_disputes} open delivery disputes.")
    reasons.append(f"Owes {naira_text(f.outstanding_kobo)} on a "
                   f"{naira_text(f.current_limit_kobo)} limit ({f.utilisation_pct}% used).")

    if f.oldest_days_overdue > LATE_DAYS:
        flags.append("overdue_past_14_days")
    if f.promises_broken >= 2:
        flags.append("promises_broken")
    if f.verified_payments >= 3 and f.on_time_pct < POOR_ON_TIME:
        flags.append("pays_late")
    if f.verified_payments < MIN_VERIFIED:
        flags.append("too_few_verified_payments")
    sf = shelf_flags(f, shelf)
    flags += sf

    if {"overdue_past_14_days", "promises_broken", "pays_late"} & set(flags):
        outcome: Outcome = "not_now"
    elif (f.verified_payments >= MIN_VERIFIED and f.on_time_pct >= SUPPORT_ON_TIME
          and f.overdue_kobo == 0 and f.promises_broken <= 1 and not f.open_disputes
          and not sf):
        outcome = "support"
    else:
        outcome = "hold"

    if shelf is not None and shelf.usable and shelf.items:
        reasons.append(_shelf_line(shelf, about_kobo(shelf.total_value_kobo)))
        for flag in sf:
            sku = flag.split(":", 1)[1]
            name = next((i.name for i in shelf.items if i.sku == sku), sku)
            reasons.append(f"The shelf shows more {name} than the distributor delivered in "
                           "4 weeks: stock may be from elsewhere or borrowed.")
    elif shelf is not None:
        reasons.append("No shelf count: the photo could not be used.")
    else:
        reasons.append("No shelf photo: history only.")

    strong = f.verified_payments >= 8
    count_ok = shelf is not None and shelf.usable and shelf.confidence != "low"
    if outcome == "hold" or not (strong or count_ok):
        confidence: Confidence = "low" if f.verified_payments < MIN_VERIFIED else "medium"
    elif strong and count_ok and not sf:
        confidence = "high"
    else:
        confidence = "medium"
    if outcome == "not_now" and strong:
        confidence = "high"
    return Recommendation(outcome, confidence, tuple(reasons), tuple(flags))
