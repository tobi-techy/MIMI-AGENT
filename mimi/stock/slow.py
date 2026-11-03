"""Slow stock and near-expiry (sales tracking, 3 Nov 2026): pure code, no model.

Low stock (``trigger.py``) watches a shelf running out. This watches the other
way: stock piling up, or sitting until it expires. Distributors told us
returns cost them most: goods are picked back up when they are expiring or
not selling, so they pay twice (deliver, then collect). Seeing it early means
the goods can still be moved to a shop that sells them, pushed, or collected
while they are still good to resell.

``assess(est, history)`` reads the estimator's figures (``estimator.py``) and
the batches the shop still holds:

* **What is left, by batch**: the units left are the newest deliveries (shops
  sell oldest first), each with its expiry day when the delivery note gave it.
* **Near expiry**: walking the batches oldest first at the sell rate, the units
  that will not sell before ``SAFETY_DAYS`` ahead of their expiry (shops pull
  goods a few days before the date; customers check). Flagged when at least
  one unit is at risk and the batch expires within ``EXPIRY_HORIZON_DAYS``.
  With no sell rate at all, every unit of a batch expiring in that horizon is
  at risk.
* **Overstock**: days of cover (units left over the sell rate) above
  ``OVERSTOCK_FACTOR`` times the shop's normal cover (its van cadence plus a
  day) and at least ``OVERSTOCK_EXTRA_DAYS`` more than normal.

Confidence is never higher than the estimate's, and at most ``medium`` unless
a shop count from the last ``COUNT_FRESH_DAYS`` days anchors it: an estimate
from deliveries alone does not know a line has slowed (a shop that stopped
selling simply stops reordering), so the low-stock question's count is what
usually reveals slow stock. Bands, never fake precision.

``pick_slow`` applies the same discipline as low stock: at most one signal per
item per ISO week and at most ``SHOP_WEEKLY_CAP`` per shop per week, the most
value at risk first. Mimi never moves stock or money: a signal is information
for the distributor, and any action is a draft the distributor approves
(``swap.py``).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from mimi.stock.estimator import COUNT_RATE_WEIGHT, Confidence, StockEstimate, estimate
from mimi.stock.history import ItemHistory, StockCount

SLOW_POLICY_VERSION = "slow-stock-v0.1"
SAFETY_DAYS = 3
EXPIRY_HORIZON_DAYS = 60
OVERSTOCK_FACTOR = 2.5
OVERSTOCK_EXTRA_DAYS = 14
MIN_UNITS = 2
COUNT_FRESH_DAYS = 14
SHOP_WEEKLY_CAP = 1
IMPLIED_MIN_DAYS = 3

SlowKind = Literal["near_expiry", "overstock"]
ExpiryBand = Literal["under_14_days", "14_to_30_days", "over_30_days", "unknown"]
CoverBand = Literal["under_2_weeks", "2_to_4_weeks", "4_to_8_weeks", "over_8_weeks", "no_sales"]
_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def expiry_band(days: int | None) -> ExpiryBand:
    if days is None:
        return "unknown"
    return "under_14_days" if days < 14 else "14_to_30_days" if days <= 30 else "over_30_days"


def cover_band(days: float | None) -> CoverBand:
    if days is None:
        return "no_sales"
    if days < 14:
        return "under_2_weeks"
    return "2_to_4_weeks" if days < 28 else "4_to_8_weeks" if days < 56 else "over_8_weeks"


@dataclass(frozen=True)
class Batch:
    """Units of one delivery still on the shelf (an estimate, by FIFO)."""

    day: int
    units: int
    expires_day: int | None
    batch: str = ""


def remaining_batches(history: ItemHistory, units_left: int, as_of_day: int) -> list[Batch]:
    """The units left, assigned to the newest deliveries before ``as_of_day``;
    oldest first."""
    out: list[Batch] = []
    left = units_left
    for s in sorted((s for s in history.stock_in if s.day < as_of_day),
                    key=lambda s: (s.day, s.delivery_id), reverse=True):
        if left <= 0:
            break
        take = min(left, s.quantity)
        out.append(Batch(s.day, take, s.expires_day, s.batch))
        left -= take
    if left > 0 and out:
        # More on the shelf than the window's deliveries (a typed count): the
        # extra is the oldest stock, with the oldest known expiry.
        oldest = out[-1]
        out[-1] = Batch(oldest.day, oldest.units + left, oldest.expires_day, oldest.batch)
    return list(reversed(out))


def units_at_risk(batches: Sequence[Batch], rate: float, as_of_day: int,
                  margin: int = SAFETY_DAYS) -> tuple[int, int | None]:
    """(units that will not sell in time, the earliest expiry day among them).

    Sells the batches oldest first at ``rate`` units a day; a batch can be sold
    until ``margin`` days before it expires. Units are whole: a batch's unsold
    part is rounded down (a part unit is not a unit lost)."""
    t = 0.0
    risk = 0
    first: int | None = None
    for b in batches:
        if b.expires_day is None:
            t += b.units / rate if rate > 0 else math.inf
            continue
        window = b.expires_day - margin - as_of_day
        if rate <= 0:
            sold = 0.0
        else:
            end = min(t + b.units / rate, float(window))
            sold = max(0.0, rate * (end - t))
            t = max(t, end)
        lost = int(math.floor(b.units - sold + 1e-9))
        if lost > 0:
            risk += lost
            first = b.expires_day if first is None else min(first, b.expires_day)
    return risk, first


@dataclass(frozen=True)
class SlowAssessment:
    shop_id: str
    sku: str
    item: str
    unit: str
    as_of_day: int
    kind: SlowKind
    units_left: int
    sell_rate_per_day: float
    days_of_cover: float | None
    normal_cover_days: float
    days_to_expiry: int | None  # earliest expiry among the units left
    units_at_risk: int  # near expiry: units that will not sell in time
    excess_units: int  # what could leave the shop: at risk, or above normal cover
    confidence: Confidence
    anchor: str  # "count" or "last_delivery"
    expiry_band: ExpiryBand
    cover_band: CoverBand
    policy_version: str = SLOW_POLICY_VERSION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cap(c: Confidence, top: Confidence) -> Confidence:
    return c if _ORDER[c] <= _ORDER[top] else top


def slow_confidence(est: StockEstimate) -> Confidence:
    fresh = (est.anchor == "count" and est.last_count_day is not None
             and est.as_of_day - est.last_count_day <= COUNT_FRESH_DAYS)
    if est.rate_source in ("one_delivery", "none"):
        return "low"
    return est.confidence if fresh else _cap(est.confidence, "medium")


def normal_cover(est: StockEstimate) -> float:
    gap = est.cover_gap_days or est.usual_gap_days or 7.0
    return float(gap) + 1.0


def count_implied_rate(est: StockEstimate, history: ItemHistory,
                       counts: Sequence[StockCount]) -> float | None:
    """The sell rate a fresh count implies since the delivery before it.

    The reorder rhythm says what a shop *used* to sell; a shop that stopped
    selling a line simply stops reordering, so that rate stays high. A count
    taken at least ``IMPLIED_MIN_DAYS`` after the last delivery (and with no
    delivery since) bounds it:
    the shelf just before that delivery (the estimate then) plus what came
    since, less the count, over the days between."""
    if est.anchor != "count" or est.last_count_day is None:
        return None
    c_day = est.last_count_day
    before = [s for s in history.stock_in if s.day <= c_day]
    if not before or any(s.day > c_day and s.day < est.as_of_day for s in history.stock_in):
        return None
    d_day = before[-1].day
    if c_day - d_day < IMPLIED_MIN_DAYS:
        return None
    prior = estimate(history, [c for c in counts if c.day < d_day], d_day)
    shelf = prior.estimated_units_left if prior is not None else 0
    inflow = sum(s.quantity for s in before if s.day == d_day)
    sold = shelf + inflow - est.anchor_units
    return max(0.0, sold / (c_day - d_day))


def assess(est: StockEstimate, history: ItemHistory,
           counts: Sequence[StockCount] = ()) -> SlowAssessment | None:
    """``None`` when the line is neither near expiry nor overstocked. With the
    counts the estimate used, a fresh count can lower the sell rate
    (``count_implied_rate``), never raise it."""
    left = est.estimated_units_left
    if left < MIN_UNITS:
        return None
    day = est.as_of_day
    rate = est.sell_rate_per_day
    implied = count_implied_rate(est, history, counts) if counts else None
    if implied is not None and implied < rate:
        # One count is a hint that the line slowed, so it is blended like the
        # estimator blends a count rate (COUNT_RATE_WEIGHT). When the shop has
        # also let a usual reorder pass (no delivery for longer than its van
        # cadence), the two agree and the count's rate is taken.
        since = day - max(s.day for s in history.stock_in if s.day < day)
        w = 1.0 if since > normal_cover(est) else COUNT_RATE_WEIGHT
        rate = round((1 - w) * rate + w * implied, 4)
    batches = remaining_batches(history, left, day)
    known = [b.expires_day for b in batches if b.expires_day is not None]
    to_expiry = (min(known) - day) if known else None
    risk, first = units_at_risk(batches, rate, day)
    cover = round(left / rate, 1) if rate > 0 else None
    normal = normal_cover(est)
    kind: SlowKind | None = None
    excess = 0
    if risk >= 1 and first is not None and first - day <= EXPIRY_HORIZON_DAYS:
        kind, excess = "near_expiry", risk
        to_expiry = first - day
    elif cover is not None and cover > OVERSTOCK_FACTOR * normal and (
            cover - normal >= OVERSTOCK_EXTRA_DAYS):
        kind = "overstock"
        excess = int(math.floor(left - rate * normal))
    if kind is None or excess < 1:
        return None
    return SlowAssessment(
        shop_id=est.shop_id, sku=est.sku, item=history.name, unit=history.unit,
        as_of_day=day, kind=kind, units_left=left, sell_rate_per_day=rate,
        days_of_cover=cover, normal_cover_days=normal, days_to_expiry=to_expiry,
        units_at_risk=risk, excess_units=excess, confidence=slow_confidence(est),
        anchor=est.anchor, expiry_band=expiry_band(to_expiry), cover_band=cover_band(cover))


# ------------------------------------------------------------ caps


@dataclass
class SlowBook:
    """Which slow items were signalled in which week."""

    item_weeks: set[tuple[str, str, str]] = field(default_factory=set)
    shop_weeks: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))

    def record(self, shop_id: str, sku: str, week: str) -> None:
        self.item_weeks.add((shop_id, sku, week))
        self.shop_weeks[(shop_id, week)] += 1


def should_signal(a: SlowAssessment, book: SlowBook, week: str) -> tuple[bool, str]:
    if (a.shop_id, a.sku, week) in book.item_weeks:
        return False, "signalled_this_week"
    if book.shop_weeks.get((a.shop_id, week), 0) >= SHOP_WEEKLY_CAP:
        return False, "shop_weekly_cap"
    return True, a.kind


def pick_slow(assessments: Sequence[SlowAssessment], book: SlowBook, week: str,
              price_kobo: dict[str, int] | None = None) -> list[SlowAssessment]:
    """The slow items to signal now: near expiry first, then the most value
    (or units) at risk; within the caps."""
    prices = price_kobo or {}
    ranked = sorted(assessments, key=lambda a: (
        a.kind != "near_expiry", -a.excess_units * prices.get(a.sku, 1),
        a.days_to_expiry if a.days_to_expiry is not None else 10**6, a.shop_id, a.sku))
    out = []
    for a in ranked:
        ok, _ = should_signal(a, book, week)
        if ok:
            book.record(a.shop_id, a.sku, week)
            out.append(a)
    return out
