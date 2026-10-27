"""Estimator v0 (RAI-270, RAI-294): stock in minus estimated sales, in code.

``estimate(history, counts, as_of_day)`` is a pure function: no clock, no
randomness, no model. The same inputs always give the same estimate.

* **Sell rate** from the last 4 weeks of deliveries and reorder gaps: a
  shop reorders to replace what it sold since the last reorder, so the units
  delivered after the first delivery in the window (plus the one just before
  it), up to and including the last, over the days between the first and the
  last ("10 cartons every 5 days is about 2 a day"). With two counts at least 5 days apart,
  the sales the counts imply (count, plus deliveries, minus the next count)
  are blended in. One delivery and nothing else: its units over 28 days
  (low confidence).
* **Units left**: from the latest count when there is one in the last 28
  days (the count overrides the estimate), else from the last delivery: a
  shop restocks to last until its next usual reorder plus about a day, so
  the shelf after a delivery is the larger of what came and the sell rate
  times (the days to the next van + 1 day). The days to the next van are the
  shop's visit cadence when the route gives it, else the item's usual
  reorder gap. Plus deliveries since the anchor,
  minus the sell rate times the days since. A tapped count anchors until the
  next delivery; a typed exact number anchors after deliveries too.
* **Days to run out**: units left over the sell rate.
* **Confidence**: from data volume (deliveries in the window) and time since
  the last count: low, medium or high.

Quantities are whole units of the SKU's pack; the sell rate is units a day
(a float, never money). Day numbers are the world's (``ItemHistory``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from mimi.stock.history import ItemHistory, StockCount, StockIn

Confidence = Literal["low", "medium", "high"]
POLICY_VERSION = "stock-estimator-v0.2"
WINDOW_DAYS = 28
MIN_GAP_DAYS = 3
MIN_REORDERS = 3
COUNT_RATE_MIN_GAP = 5
COUNT_RATE_WEIGHT = 0.5
COVER_EXTRA_DAYS = 1.0
# A tapped bucket (or the empty shelf a WhatsApp reorder implies) anchors the
# estimate until the next delivery; a typed exact number anchors it after
# deliveries too. Tuned on the dev seed only (docs/STOCK.md).
EXACT_SOURCES = ("typed",)


@dataclass(frozen=True)
class StockEstimate:
    shop_id: str
    sku: str
    as_of_day: int
    units_in_window: int
    deliveries_in_window: int
    sell_rate_per_day: float
    rate_source: str  # "reorder_gaps", "reorder_gaps+counts", "one_delivery", "none"
    anchor: str  # "count" or "last_delivery"
    anchor_day: int
    anchor_units: int
    # The shelf assumed right after the anchoring delivery (>= what came).
    assumed_after_anchor: float
    usual_gap_days: float | None
    # The days the shelf after a delivery is assumed to cover, before the
    # extra day: the shop's van cadence when known, else ``usual_gap_days``.
    cover_gap_days: float | None
    units_in_since_anchor: int
    estimated_units_left: int
    days_to_run_out: float | None
    confidence: Confidence
    last_count_day: int | None
    policy_version: str = POLICY_VERSION

    @property
    def run_out_day(self) -> int | None:
        """The world day the shelf is expected to end empty, if nothing more
        is delivered (``as_of_day`` when it is already empty)."""
        if self.days_to_run_out is None:
            return None
        return self.as_of_day + max(0, math.ceil(self.days_to_run_out) - 1)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "run_out_day": self.run_out_day}


def _in_before(stock_in: Sequence[StockIn], day: int) -> list[StockIn]:
    return [s for s in stock_in if s.day < day]


def reorder_rate(stock_in: Sequence[StockIn], as_of_day: int) -> tuple[float, str]:
    """Units a day from the reorder rhythm of the last 4 weeks."""
    seen = _in_before(stock_in, as_of_day)
    window = [s for s in seen if s.day >= as_of_day - WINDOW_DAYS]
    before = [s for s in seen if s.day < as_of_day - WINDOW_DAYS]
    # Reach back for the reorder just before the window, and further (up to
    # 8 weeks back) while there are fewer than MIN_REORDERS delivery days.
    if before:
        window = [before.pop(), *window]
    while before and before[-1].day >= as_of_day - 2 * WINDOW_DAYS and (
        len({s.day for s in window}) < MIN_REORDERS
    ):
        window = [before.pop(), *window]
    # One delivery a day at most counts as one reorder (same-day top-ups merge).
    merged: dict[int, int] = {}
    for s in window:
        merged[s.day] = merged.get(s.day, 0) + s.quantity
    days = sorted(merged)
    if len(days) >= 2 and days[-1] - days[0] >= MIN_GAP_DAYS:
        # Each reorder replaces what sold since the one before it, so the
        # units sold between the first and last delivery are the units of
        # every delivery after the first. (v0.1 summed every delivery but the
        # last, which assumes an empty shelf at each delivery; it ran 0.85x
        # the true rate on 14-day routes, where the first delivery in a long
        # window is often a small top-up. Dev seed only, docs/STOCK.md.)
        sold = sum(merged[d] for d in days[1:])
        return sold / (days[-1] - days[0]), "reorder_gaps"
    if days:
        return sum(merged.values()) / WINDOW_DAYS, "one_delivery"
    return 0.0, "none"


def count_rate(
    stock_in: Sequence[StockIn], counts: Sequence[StockCount], as_of_day: int
) -> float | None:
    """Units a day the shop's own counts imply: the last two counts at least
    5 days apart in the last 8 weeks."""
    usable = [c for c in counts if as_of_day - 2 * WINDOW_DAYS <= c.day <= as_of_day]
    if len(usable) < 2:
        return None
    last = usable[-1]
    for first in reversed(usable[:-1]):
        gap = last.day - first.day
        if gap < COUNT_RATE_MIN_GAP:
            continue
        inflow = sum(
            s.quantity
            for s in stock_in
            if _after_count(s, first) and not _after_count(s, last)
        )
        sold = first.units + inflow - last.units
        return max(0.0, sold / gap)
    return None


def _after_count(s: StockIn, c: StockCount) -> bool:
    """A delivery the count did not include."""
    return s.day > c.day or (s.day == c.day and not c.after_delivery)


def implied_counts(stock_in: Sequence[StockIn], as_of_day: int) -> list[StockCount]:
    """A WhatsApp reorder between van visits says the line ran out that day:
    a count of 0 on the next morning, before the reorder arrives."""
    out = []
    for s in stock_in:
        if s.source == "whatsapp_reorder" and s.ordered_day is not None:
            if s.ordered_day + 1 <= as_of_day:
                out.append(StockCount(s.ordered_day + 1, "0-2", 0, source="reorder"))
    return out


def usual_gap(stock_in: Sequence[StockIn], as_of_day: int) -> float | None:
    """The median days between delivery days in the window (plus the one
    before it): the shop's reorder rhythm."""
    seen = _in_before(stock_in, as_of_day)
    window = [s for s in seen if s.day >= as_of_day - WINDOW_DAYS]
    before = [s for s in seen if s.day < as_of_day - WINDOW_DAYS]
    days = sorted({s.day for s in ([before[-1]] if before else []) + window})
    gaps = [b - a for a, b in zip(days, days[1:], strict=False)]
    if not gaps:
        return None
    gaps.sort()
    mid = len(gaps) // 2
    return float(gaps[mid]) if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2


def confidence_for(deliveries_in_window: int, days_since_count: int | None) -> Confidence:
    points = 2 if deliveries_in_window >= 3 else 1 if deliveries_in_window == 2 else 0
    if days_since_count is not None:
        points += 2 if days_since_count <= 7 else 1 if days_since_count <= 14 else 0
    if points >= 4:
        return "high"
    if points >= 2:
        return "medium"
    return "low"


def estimate(
    history: ItemHistory, counts: Sequence[StockCount], as_of_day: int
) -> StockEstimate | None:
    """The estimate on the morning of ``as_of_day`` from deliveries before it
    and counts taken up to it. ``None`` when nothing was ever delivered."""
    seen = _in_before(history.stock_in, as_of_day)
    if not seen:
        return None
    counts = sorted(
        [*(c for c in counts if c.day <= as_of_day), *implied_counts(history.stock_in, as_of_day)],
        key=lambda c: (c.day, c.after_delivery),
    )
    window = [s for s in seen if s.day >= as_of_day - WINDOW_DAYS]
    rate, source = reorder_rate(seen, as_of_day)
    crate = count_rate(seen, counts, as_of_day)
    if crate is not None and source == "reorder_gaps":
        rate = (1 - COUNT_RATE_WEIGHT) * rate + COUNT_RATE_WEIGHT * crate
        source = "reorder_gaps+counts"
    elif crate is not None:
        rate, source = crate, "counts"
    last_count = counts[-1] if counts else None
    gap = usual_gap(seen, as_of_day)
    cover_gap = float(history.visit_days) if history.visit_days else gap
    last_delivery_day = seen[-1].day
    count_is_latest = last_count is not None and (
        last_count.day > last_delivery_day
        or (last_count.day == last_delivery_day and last_count.after_delivery)
    )
    if (
        last_count is not None
        and last_count.day >= as_of_day - WINDOW_DAYS
        and (count_is_latest or last_count.source in EXACT_SOURCES)
    ):
        anchor, anchor_day, anchor_units = "count", last_count.day, last_count.units
        after = float(anchor_units)
        since = [s for s in seen if _after_count(s, last_count)]
    else:
        last = seen[-1]
        same_day = [s for s in seen if s.day == last.day]
        anchor, anchor_day = "last_delivery", last.day
        anchor_units = sum(s.quantity for s in same_day)
        after = float(anchor_units)
        if cover_gap is not None and rate > 0:
            after = max(after, rate * (cover_gap + COVER_EXTRA_DAYS))
        since = []
    units_since = sum(s.quantity for s in since)
    left = after + units_since - rate * (as_of_day - anchor_day)
    left = max(0.0, left)
    days = round(left / rate, 1) if rate > 0 else None
    since_count = as_of_day - last_count.day if last_count is not None else None
    return StockEstimate(
        shop_id=history.shop_id,
        sku=history.sku,
        as_of_day=as_of_day,
        units_in_window=sum(s.quantity for s in window),
        deliveries_in_window=len({s.day for s in window}),
        sell_rate_per_day=round(rate, 4),
        rate_source=source,
        anchor=anchor,
        anchor_day=anchor_day,
        anchor_units=anchor_units,
        assumed_after_anchor=round(after, 3),
        usual_gap_days=gap,
        cover_gap_days=cover_gap,
        units_in_since_anchor=units_since,
        estimated_units_left=int(math.floor(left + 0.5)),
        days_to_run_out=days,
        confidence=confidence_for(len({s.day for s in window}), since_count),
        last_count_day=last_count.day if last_count is not None else None,
    )
