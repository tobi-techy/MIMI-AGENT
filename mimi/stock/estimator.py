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

import hashlib
import json
import math
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from fractions import Fraction
from typing import Any, Literal

from mimi.provenance import POLICY_VERSION as PROOF_POLICY
from mimi.provenance import stamp as proof_stamp
from mimi.provenance import witness as proof_witness
from mimi.stock.history import ItemHistory, StockCount, StockIn, bucket_of
from mimi.stock.items import pack_size

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


# =====================================================================
# v0.3 record layer (RAI-294): one ``stock_estimates`` row per shop and item
# =====================================================================
#
# ``estimate_row`` wraps ``estimate`` (the v0.2 numbers the backtest gates)
# and adds what a stored record needs: base units (RAI-154), confirmed stock
# in only, "not enough history" under two deliveries, a numeric confidence
# that decays every day without a new count, the proof level (always
# ``estimated``, RAI-295), the input ids behind every figure, a run-out date
# and the money tied up in stock. Still pure: same inputs, same row.

RECORD_POLICY = "stock-estimates-v0.3"
CountKind = Literal["tap", "typed", "voice", "book", "pos", "connected_tool"]
EXACT_KINDS: tuple[str, ...] = ("typed", "voice", "book", "pos", "connected_tool")
BUCKET_RANGE: dict[str, tuple[int, int | None]] = {"0-2": (0, 2), "3-5": (3, 5), "6+": (6, None)}
# Confidence (proposed in RAI-294, to tune in the backtest).
CONF_EXACT = 1.0
CONF_BUCKET = 0.7
CONF_NO_COUNT = 0.6
CONF_DECAY_PER_DAY = 0.04
CONF_UNEVEN_DROP = 0.15
UNEVEN_GAP_CV = 0.5
MIN_DELIVERIES = 2
# The words a person reads; never the number alone.
ConfidenceWord = Literal["High", "Medium", "Low"]
# Words a shop or rep may use for the smaller unit inside a pack.
INNER_UNITS = ("piece", "pieces", "pc", "pcs", "packet", "packets", "sachet", "sachets",
               "unit", "units")


def confidence_word(score: float) -> ConfidenceWord:
    return "High" if score >= 0.7 else "Medium" if score >= 0.4 else "Low"


@dataclass(frozen=True)
class UnitBook:
    """How one product converts to its base unit (RAI-154): the SKU's own
    unit (``ctn``) holds ``pieces`` base units; the inner unit is one each."""

    product_id: str
    unit: str
    pieces: int

    @classmethod
    def for_sku(cls, product_id: str, name: str, unit: str) -> UnitBook:
        return cls(product_id, unit, pack_size(name) or 1)

    def to_base(self, quantity: Fraction | int | float | str, unit: str) -> int | None:
        """Base units, or None for a unit we do not know (or a part of a
        piece): no guess is ever made."""
        q = Fraction(str(quantity))
        u = unit.strip().lower()
        if q < 0:
            return None
        if u in (self.unit.lower(), "", f"{self.unit.lower()}s") or _same_unit(u, self.unit):
            base = q * self.pieces
        elif self.pieces > 1 and u in INNER_UNITS:
            base = q
        else:
            return None
        return int(base) if base.denominator == 1 else None


_UNIT_ALIASES = {"ctn": ("carton", "cartons", "ctns"), "bag": ("bags",), "pk": ("pack", "packs"),
                 "btl": ("bottle", "bottles"), "tin": ("tins",), "pc": ("piece", "pieces"),
                 "tub": ("tubs",), "keg": ("kegs",)}


def _same_unit(u: str, unit: str) -> bool:
    return u in _UNIT_ALIASES.get(unit.lower(), ())


@dataclass(frozen=True)
class DeliveryIn:
    """Stock in: a delivery line (or an other-supplier buy the shop reported)."""

    input_id: str
    day: int
    quantity: Fraction | int | float | str
    unit: str
    confirmed: bool = True
    source: Literal["distributor", "other_supplier"] = "distributor"


@dataclass(frozen=True)
class CountIn:
    """A count of any kind: a one-tap bucket, a typed or voice number, a book
    read, POS or a connected tool. A count always overrides the estimate."""

    input_id: str
    day: int
    kind: CountKind
    bucket: str | None = None
    quantity: Fraction | int | float | str | None = None
    unit: str = ""
    after_delivery: bool = False


@dataclass(frozen=True)
class EstimateRow:
    """One ``stock_estimates`` row. Quantities are in the product's own unit
    (cartons, packs) as decimals for display ("about 3.5 cartons"); the maths
    ran in base units."""

    shop_id: str
    product_id: str
    unit: str
    as_of_day: int
    status: Literal["ok", "not_enough_history", "unknown_unit", "no_stock_in"]
    anchor_qty: float | None
    anchor_range: str | None  # the bucket a one-tap count gave ("0-2")
    anchor_day: int | None
    anchor_source: str  # count kind, "last_delivery" or ""
    stock_in_since_anchor: float | None
    est_out_since_anchor: float | None
    est_left: float | None
    est_left_raw: float | None  # before flooring at zero
    est_daily_rate: float | None
    rate_source: str
    days_to_run_out: float | None
    run_out_date: str | None  # Africa/Lagos calendar date, ISO
    confidence: float
    confidence_word: ConfidenceWord
    money_tied_kobo: int | None  # est_left x cost price, estimated
    proof_level: str
    witnesses: str
    proof_policy_version: str
    inputs: tuple[str, ...]
    policy_version: str
    inputs_hash: str

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["inputs"] = list(self.inputs)
        return d


def _hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def estimate_row(
    shop_id: str,
    units: UnitBook,
    deliveries: Sequence[DeliveryIn],
    counts: Sequence[CountIn],
    as_of_day: int,
    *,
    day0: date,
    cost_kobo: int = 0,
    visit_days: int | None = None,
    name: str = "",
) -> EstimateRow:
    """The record for one shop and item on the morning of ``as_of_day``."""
    used_in = [d for d in deliveries if d.confirmed and d.day < as_of_day]
    used_counts = [c for c in counts if c.day <= as_of_day]
    input_ids = tuple(sorted({d.input_id for d in used_in} | {c.input_id for c in used_counts}))
    inputs_hash = _hash({"shop": shop_id, "product": units.product_id,
                         "policy": RECORD_POLICY, "estimator": POLICY_VERSION,
                         "proof": PROOF_POLICY, "as_of_day": as_of_day, "cost": cost_kobo,
                         "visit": visit_days, "unit": [units.unit, units.pieces],
                         "in": sorted((d.input_id, d.day, str(d.quantity), d.unit, d.source)
                                      for d in used_in),
                         "counts": sorted((c.input_id, c.day, c.kind, c.bucket, str(c.quantity),
                                           c.unit, c.after_delivery) for c in used_counts)})
    st = proof_stamp("stock_estimate", [proof_witness("estimator", f"{shop_id}:{units.product_id}"),
                                        *(proof_witness("shop_count", c.input_id)
                                          for c in used_counts)])

    def empty(status: str, conf: float = 0.0) -> EstimateRow:
        return EstimateRow(shop_id, units.product_id, units.unit, as_of_day, status,  # type: ignore[arg-type]
                           None, None, None, "", None, None, None, None, None, "none", None,
                           None, round(conf, 2), confidence_word(conf), None, st.proof_level,
                           st.witnesses_json(), st.proof_policy_version, input_ids,
                           RECORD_POLICY, inputs_hash)

    stock_in: list[StockIn] = []
    for d in sorted(used_in, key=lambda d: (d.day, d.input_id)):
        base = units.to_base(d.quantity, d.unit)
        if base is None:
            return empty("unknown_unit")
        stock_in.append(StockIn(d.day, base, d.input_id))
    if not stock_in:
        return empty("no_stock_in")
    scounts: list[StockCount] = []
    for c in sorted(used_counts, key=lambda c: (c.day, c.after_delivery, c.input_id)):
        if c.kind == "tap":
            if c.bucket not in BUCKET_RANGE:
                return empty("unknown_unit")
            # The bucket's middle for the maths; the range is kept for display.
            # For "6+" the caller may pass the units it assumed (the estimate
            # when already 6 or more, ``history.assumed_units``).
            mid_base = {"0-2": 1, "3-5": 4, "6+": 6}[c.bucket] * units.pieces
            if c.bucket == "6+" and c.quantity is not None:
                given = units.to_base(c.quantity, c.unit)
                if given is None:
                    return empty("unknown_unit")
                mid_base = max(mid_base, given)
            scounts.append(StockCount(c.day, c.bucket, mid_base,  # type: ignore[arg-type]
                                      after_delivery=c.after_delivery, source="tap"))
        else:
            base = units.to_base(c.quantity if c.quantity is not None else -1, c.unit)
            if base is None:
                return empty("unknown_unit")
            scounts.append(StockCount(c.day, bucket_of(base // units.pieces), base,
                                      after_delivery=c.after_delivery, source="typed"))
    hist = ItemHistory(shop_id, units.product_id, name, units.unit, tuple(stock_in),
                       visit_days=visit_days)
    e = estimate(hist, scounts, as_of_day)
    assert e is not None  # stock_in is not empty
    per = float(units.pieces)
    delivery_days = sorted({s.day for s in stock_in})
    # Confidence: where it starts, then down a step every day since.
    last_count = next((c for c in reversed(sorted(used_counts, key=lambda c: (
        c.day, c.after_delivery, c.input_id)))), None)
    counted = e.anchor == "count" and last_count is not None
    source: str
    if counted:
        assert last_count is not None
        start = CONF_EXACT if last_count.kind in EXACT_KINDS else CONF_BUCKET
        source = last_count.kind
        arange = last_count.bucket if last_count.kind == "tap" else None
    else:
        start, source, arange = CONF_NO_COUNT, "last_delivery", None
    conf = start - CONF_DECAY_PER_DAY * max(0, as_of_day - e.anchor_day)
    gaps = [b - a for a, b in zip(delivery_days, delivery_days[1:], strict=False)]
    if len(gaps) >= 2 and statistics.mean(gaps) > 0 and (
            statistics.pstdev(gaps) / statistics.mean(gaps) > UNEVEN_GAP_CV):
        conf -= CONF_UNEVEN_DROP
    conf = max(0.0, min(1.0, conf))
    if len(delivery_days) < MIN_DELIVERIES:
        row = empty("not_enough_history", conf)
        return EstimateRow(**{**asdict(row), "inputs": row.inputs,
                              "anchor_qty": round(e.assumed_after_anchor / per, 2)
                              if counted else None,
                              "anchor_range": arange, "anchor_day": e.anchor_day,
                              "anchor_source": source})
    rate = e.sell_rate_per_day
    out_since = rate * (as_of_day - e.anchor_day)
    raw_left = e.assumed_after_anchor + e.units_in_since_anchor - out_since
    left = max(0.0, raw_left)
    days = e.days_to_run_out
    run_out = None
    if e.run_out_day is not None:
        run_out = (day0 + timedelta(days=e.run_out_day)).isoformat()
    money = int(math.floor(left * cost_kobo / per)) if cost_kobo else None
    return EstimateRow(
        shop_id=shop_id, product_id=units.product_id, unit=units.unit, as_of_day=as_of_day,
        status="ok", anchor_qty=round(e.assumed_after_anchor / per, 2), anchor_range=arange,
        anchor_day=e.anchor_day, anchor_source=source,
        stock_in_since_anchor=round(e.units_in_since_anchor / per, 2),
        est_out_since_anchor=round(out_since / per, 2), est_left=round(left / per, 2),
        est_left_raw=round(raw_left / per, 2), est_daily_rate=round(rate / per, 4),
        rate_source=e.rate_source, days_to_run_out=days, run_out_date=run_out,
        confidence=round(conf, 2), confidence_word=confidence_word(conf),
        money_tied_kobo=money, proof_level=st.proof_level, witnesses=st.witnesses_json(),
        proof_policy_version=st.proof_policy_version, inputs=input_ids,
        policy_version=RECORD_POLICY, inputs_hash=inputs_hash)


def count_wanted(row: EstimateRow, *, lead_time_days: int, top_item: bool) -> bool:
    """The low-item trigger: ask for a one-tap count when the item may run
    out within the lead time plus two days, or a top item's confidence is
    Low. The weekly cap (one ask per shop) is the caller's (``StockBook``)."""
    if row.status != "ok":
        return False
    soon = row.days_to_run_out is not None and row.days_to_run_out <= lead_time_days + 2
    return soon or (top_item and row.confidence_word == "Low")
