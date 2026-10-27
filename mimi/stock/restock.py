"""The restock draft: quantity and amount in code, checked before it is sent.

``draft_quantity`` covers the shop until its usual next delivery plus a lead
day and a safety day, from the sell rate and the units left after the count,
rounded up to the distributor's usual pack size (``items.order_multiple``).
No draft when what is left lasts that long.

``verify`` is the verifier (RAI-231): the quantity is a whole number of packs,
at least one pack, at most 3x the most the shop was ever delivered in one go
(and never more than 200 units); the unit price is the catalogue price on the
day; the amount is quantity x price in integer kobo. A draft that fails is
not proposed. A draft is never an order: it waits in the distributor's
approvals, and the distributor decides (and sets any credit).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from mimi.stock.estimator import StockEstimate
from mimi.stock.history import ItemHistory
from mimi.stock.items import order_multiple
from mimi.types import Kobo
from mimi.world.model import Sku

LEAD_DAYS = 1
SAFETY_DAYS = 1
HARD_CAP_UNITS = 200


@dataclass(frozen=True)
class DraftPlan:
    quantity: int
    multiple: int
    cover_days: float
    need_units: float
    cap_units: int


def cover_days(est: StockEstimate, restock_days: int) -> float:
    gap = est.usual_gap_days if est.usual_gap_days else float(restock_days)
    return gap + LEAD_DAYS + SAFETY_DAYS


def cap_units(history: ItemHistory) -> int:
    most = max((s.quantity for s in history.stock_in), default=1)
    return min(HARD_CAP_UNITS, max(1, 3 * most))


def draft_quantity(est: StockEstimate, history: ItemHistory, restock_days: int) -> DraftPlan | None:
    """``None`` when the shelf lasts until the next usual delivery."""
    if est.sell_rate_per_day <= 0:
        return None
    days = cover_days(est, restock_days)
    if est.days_to_run_out is not None and est.days_to_run_out >= days:
        return None
    need = est.sell_rate_per_day * days - est.estimated_units_left
    if need <= 0:
        return None
    mult = order_multiple(history.unit)
    qty = max(mult, math.ceil(need / mult) * mult)
    return DraftPlan(qty, mult, days, round(need, 3), cap_units(history))


def line_amount(quantity: int, sku: Sku, day: int, rise_day: int) -> tuple[Kobo, Kobo]:
    price = sku.price_on(day, rise_day)
    return price, quantity * price


def verify(
    *,
    quantity: int,
    unit: str,
    unit_price_kobo: int,
    amount_kobo: int,
    sku: Sku,
    day: int,
    rise_day: int,
    cap: int,
) -> list[str]:
    """Every problem with a draft; empty means it may be proposed."""
    problems = []
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        problems.append("quantity: not a whole number of at least 1")
        return problems
    mult = order_multiple(unit)
    if quantity % mult:
        problems.append(f"quantity: {quantity} is not a multiple of the pack size {mult}")
    if quantity > cap:
        problems.append(f"quantity: {quantity} is over the cap of {cap}")
    want_price = sku.price_on(day, rise_day)
    if unit_price_kobo != want_price:
        problems.append(f"price: {unit_price_kobo} kobo is not the catalogue price {want_price}")
    if amount_kobo != quantity * unit_price_kobo:
        problems.append("amount: not quantity x unit price")
    return problems
