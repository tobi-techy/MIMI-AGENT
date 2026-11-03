"""The distributor's sell-through view: per shop and product, in code.

For each (shop, product) on a day:

* **sold over the period**: from the shop's own counts when there are two,
  far enough apart (``count_before + delivered between - latest count``, over
  the days between them), else the estimator's sell rate times the period.
  Each figure carries its proof level from ``mimi.provenance`` (record type
  ``sell_through``): a POS feed is verified; a shop's own count or the
  estimator is estimated, and the word says which ("estimated (shop count)").
* **days of cover**: the estimated units left over the sell rate.
* **days to expiry**: the earliest expiry among the units left (the newest
  deliveries, shops sell oldest first), when the delivery notes gave one.
* **status**: ``near_expiry`` or ``slow`` (``slow.assess`` at medium or high
  confidence, ``alerts``), else ``low`` (the low-stock rule: under 3 days
  left), else ``ok``. A low-confidence slow reading is a ``watch`` hint only.

Nothing here is shown to a shop, and no internal id goes into a display
string: names, ``₦`` with commas, plain days.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from mimi.provenance import proof_level, proof_word, witness
from mimi.stock.estimator import StockEstimate, estimate
from mimi.stock.history import ItemHistory, StockCount
from mimi.stock.slow import SlowAssessment, assess, remaining_batches
from mimi.stock.trigger import ASK_UNDER_DAYS

SellStatus = Literal["low", "ok", "slow", "near_expiry"]
STATUS_WORD: dict[str, str] = {"low": "Low", "ok": "OK", "slow": "Slow",
                               "near_expiry": "Near expiry"}
MIN_COUNT_GAP = 5
PERIOD_DAYS = 28


@dataclass(frozen=True)
class SellThrough:
    shop_id: str
    sku: str
    item: str
    unit: str
    as_of_day: int
    period_days: int
    sold_units: int
    sold_over_days: int
    sold_method: Literal["counts", "estimate"]
    proof_level: str
    proof: str
    units_left: int
    days_of_cover: float | None
    days_to_expiry: int | None
    expires_day: int | None
    status: SellStatus
    confidence: str
    last_count_day: int | None
    slow: SlowAssessment | None = None
    # A low-confidence slow or near-expiry reading: shown as a hint, no alert.
    watch: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["slow"] = self.slow.as_dict() if self.slow else None
        return out


def _count_witness(c: StockCount, shop_id: str, sku: str) -> Any:
    kind = "pos_report" if c.source == "connector" else "shop_count"
    return witness(kind, f"count:{shop_id}:{sku}:{c.day}:{c.source}")


def sold_from_counts(history: ItemHistory, counts: Sequence[StockCount], as_of_day: int,
                     period_days: int) -> tuple[int, int, list[StockCount]] | None:
    """(units sold, over days, the two counts) from the latest count and the
    latest one at least ``MIN_COUNT_GAP`` days before it inside the period
    (or the nearest before the period starts)."""
    usable = sorted((c for c in counts if c.day <= as_of_day and c.source != "reorder"),
                    key=lambda c: (c.day, c.after_delivery))
    if len(usable) < 2:
        return None
    last = usable[-1]
    start = as_of_day - period_days
    first = None
    for c in reversed(usable[:-1]):
        if last.day - c.day < MIN_COUNT_GAP:
            continue
        first = c
        if c.day <= start:
            break
    if first is None:
        return None

    def after(s_day: int, c: StockCount) -> bool:
        return s_day > c.day or (s_day == c.day and not c.after_delivery)

    inflow = sum(s.quantity for s in history.stock_in
                 if after(s.day, first) and not after(s.day, last))
    sold = max(0, first.units + inflow - last.units)
    return sold, last.day - first.day, [first, last]


def alerts(slow: SlowAssessment | None) -> bool:
    """The one gate for a slow status, a signal and a draft: an assessment
    with medium or high confidence. A low-confidence one is a ``watch``: a
    count would tell (``slow_eval`` scores the gate)."""
    return slow is not None and slow.confidence != "low"


def status_of(est: StockEstimate, slow: SlowAssessment | None) -> SellStatus:
    if alerts(slow):
        assert slow is not None
        return "near_expiry" if slow.kind == "near_expiry" else "slow"
    if est.days_to_run_out is not None and est.days_to_run_out < ASK_UNDER_DAYS:
        return "low"
    return "ok"


def sell_through(history: ItemHistory, counts: Sequence[StockCount], as_of_day: int,
                 period_days: int = PERIOD_DAYS) -> SellThrough | None:
    est = estimate(history, counts, as_of_day)
    if est is None:
        return None
    slow = assess(est, history, counts)
    by_counts = sold_from_counts(history, counts, as_of_day, period_days)
    if by_counts is not None:
        sold, over, used = by_counts
        ws = [_count_witness(c, history.shop_id, history.sku) for c in used]
        method: Literal["counts", "estimate"] = "counts"
    else:
        sold = int(math.floor(est.sell_rate_per_day * period_days + 0.5))
        over = period_days
        ws = [witness("estimator", f"estimate:{history.shop_id}:{history.sku}:{as_of_day}")]
        method = "estimate"
    level = proof_level("sell_through", ws)
    batches = remaining_batches(history, est.estimated_units_left, as_of_day)
    known = [b.expires_day for b in batches if b.expires_day is not None]
    exp_day = (slow.as_of_day + slow.days_to_expiry
               if slow is not None and slow.days_to_expiry is not None
               else (min(known) if known else None))
    cover = (round(est.estimated_units_left / est.sell_rate_per_day, 1)
             if est.sell_rate_per_day > 0 else None)
    return SellThrough(
        shop_id=history.shop_id, sku=history.sku, item=history.name, unit=history.unit,
        as_of_day=as_of_day, period_days=period_days, sold_units=sold, sold_over_days=over,
        sold_method=method, proof_level=level, proof=proof_word(level, ws),
        units_left=est.estimated_units_left, days_of_cover=cover,
        days_to_expiry=(exp_day - as_of_day) if exp_day is not None else None,
        expires_day=exp_day, status=status_of(est, slow),
        confidence=slow.confidence if slow is not None else est.confidence,
        last_count_day=est.last_count_day, slow=slow,
        watch=slow.kind if slow is not None and not alerts(slow) else "")
