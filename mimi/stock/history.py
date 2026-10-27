"""What Mimi knows about a shop's stock: exact stock in, and its counts.

Mimi never sees the shop's sales. It sees what the distributor delivered
(exact, from the rep's logged deliveries) and, now and then, the shop's own
one-tap answer to "how many are left?". Everything here is integers: whole
units of the SKU's pack (cartons, bags, packs) and world day numbers (day 0 is
the world's start date; day ``weeks * 7`` is the live day).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from mimi.world.model import Delivery, World

Bucket = Literal["0-2", "3-5", "6+"]
BUCKETS: tuple[Bucket, ...] = ("0-2", "3-5", "6+")


StockSource = Literal["van", "whatsapp_reorder"]


@dataclass(frozen=True)
class StockIn:
    """One delivered line: exact units in, from the rep's logged delivery.

    ``source`` is ``"whatsapp_reorder"`` when the shop asked for it on
    WhatsApp between van visits because the line ran out ("e don finish,
    send 6 cartons"); ``ordered_day`` is the day it asked. That message is
    itself a signal: the shelf was empty, or nearly, that day.
    """

    day: int
    quantity: int
    delivery_id: str
    source: StockSource = "van"
    ordered_day: int | None = None


@dataclass(frozen=True)
class StockCount:
    """The shop's own answer to a one-tap count, taken on ``day``.

    ``bucket`` is what the shop said; ``units`` is the whole number the
    estimator assumes for it (``assumed_units``). Kept by Mimi for the
    estimate only: the distributor never sees a shop's counts.
    """

    day: int
    bucket: Bucket
    units: int
    # The shop answered before (False) or after (True) that day's delivery.
    after_delivery: bool = False
    # "tap" (a button), "typed" (an exact number), or "reorder" (implied by a
    # WhatsApp reorder saying the line ran out: units 0, end of that day).
    source: str = "tap"


def assumed_units(bucket: Bucket, estimated: int) -> int:
    """The units the estimate resets to: the bucket's middle, or for "6+"
    the estimate when it is already 6 or more (else 6)."""
    if bucket == "0-2":
        return 1
    if bucket == "3-5":
        return 4
    return max(6, estimated)


def bucket_of(units: int) -> Bucket:
    if units < 0:
        raise ValueError("a count cannot be negative")
    if units <= 2:
        return "0-2"
    if units <= 5:
        return "3-5"
    return "6+"


@dataclass(frozen=True)
class ItemHistory:
    shop_id: str
    sku: str
    name: str
    unit: str
    stock_in: tuple[StockIn, ...]


def item_histories(
    world: World, *, upto_day: int | None = None, include_live: bool = False
) -> dict[tuple[str, str], ItemHistory]:
    """Every (shop, SKU) the shop has been delivered, with its stock-in
    events before ``upto_day`` (all of history when None)."""
    deliveries: list[Delivery] = list(world.deliveries)
    if include_live:
        deliveries += list(world.live_deliveries)
    rows: dict[tuple[str, str], list[StockIn]] = defaultdict(list)
    names: dict[str, tuple[str, str]] = {}
    for d in deliveries:
        if upto_day is not None and d.day >= upto_day:
            continue
        for line in d.lines:
            rows[(d.shop_id, line.sku)].append(StockIn(d.day, line.quantity, d.delivery_id))
            names[line.sku] = (line.name, line.unit)
    return {
        key: ItemHistory(key[0], key[1], *names[key[1]], tuple(sorted(v, key=lambda s: (s.day, s.delivery_id))))
        for key, v in sorted(rows.items())
    }
