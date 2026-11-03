"""Expiry dates and batch codes on delivered lines (sales tracking, 3 Nov 2026).

Distributors told us returns cost the most: goods are picked back up when they
are about to expire or are not selling, so the distributor pays twice (deliver,
then collect). To see that coming Mimi needs the expiry date of what it
delivered. A real distributor has it on the delivery note or the batch label;
the synthetic Coral Lane world did not, so this module adds it as an overlay,
deterministic from the world's seed, without changing the committed world
fixture (``python -m mimi.world --check`` still holds and no recorded eval
moves):

* every delivered line gets a batch code and an expiry day: the SKU's usual
  shelf life (``SHELF_LIFE_DAYS``, realistic for each product kind) times the
  share of it left when the batch reached the shop (35% to 90%, from a hash
  of the delivery and SKU), so a carton of noodles usually has months left
  and a bag of sachet water a few weeks;
* about 5% of lines are short-dated batches (14 to 35 days left), the kind a
  distributor pushes out to clear a warehouse;
* ``seeded_short_dated`` picks, for the demo, the few lines that sell slowest
  at their shop on the live day (from the estimator, no randomness) and gives
  their latest batch 9 to 20 days left (or 26 to 40, so an early pickup is
  still possible), so the slow-stock scene has real cases
  to show. These are seeded on purpose and documented in docs/STOCK.md.

Days are world day numbers (day 0 is the world's start date).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mimi.world.model import World

EXPIRY_POLICY = "expiry-overlay-v1"

# Shelf life from manufacture, in days, by SKU (typical Nigerian FMCG labels).
SHELF_LIFE_DAYS: Mapping[str, int] = {
    "NDL-70-CTN": 240, "NDL-120-CTN": 240,
    "MLK-SCH-BAG": 270, "MLK-SCH-CTN": 270, "MLK-TIN-CTN": 540,
    "OIL-1L": 365, "OIL-5L": 365,
    "RICE-50KG": 540, "RICE-5KG": 540,
    "SUG-1KG": 720, "SUG-50KG": 720,
    "CUBE-100": 540, "CUBE-CTN": 540,
    "DET-900G": 720, "DET-SCH-12": 720, "SOAP-CTN": 1080,
    "SPAG-500": 540, "SPAG-CTN": 540, "SEMO-1KG": 270,
    "TPASTE-CTN": 720, "TPASTE-140": 720,
    "SARD-125": 730, "COCOA-500": 365, "MALTED-400": 365,
    "SODA-50CL": 180, "MALT-33CL": 270, "WATER-75CL": 365, "PWATER-BAG": 30,
    "BISC-CTN": 180, "CUSTARD-2KG": 365,
}
DEFAULT_SHELF_LIFE = 365
LEFT_MIN, LEFT_MAX = 0.35, 0.90
SHORT_DATED_SHARE = 0.05
SHORT_DATED_DAYS = (14, 35)
# Demo seeding: how many slow lines get a near-expiry batch, and its days left.
SEEDED_LINES = 8
SEEDED_DAYS = (9, 20)
SEEDED_RESELL_DAYS = (26, 40)


@dataclass(frozen=True)
class LineExpiry:
    expires_day: int
    batch: str
    short_dated: bool = False
    seeded: bool = False


def _unit(*parts: object) -> float:
    """A number in [0, 1) from a hash of ``parts``: same inputs, same number."""
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return int(digest[:12], 16) / float(16**12)


def batch_code(sku: str, delivery_id: str, seed: int) -> str:
    prefix = sku.split("-")[0][:4]
    tail = hashlib.sha256(f"batch|{seed}|{delivery_id}|{sku}".encode()).hexdigest()[:5].upper()
    return f"{prefix}-{tail}"


def line_expiry(seed: int, delivery_id: str, sku: str, day: int) -> LineExpiry:
    """The batch and expiry day of one delivered line (no seeding)."""
    batch = batch_code(sku, delivery_id, seed)
    if _unit("short", seed, delivery_id, sku) < SHORT_DATED_SHARE:
        lo, hi = SHORT_DATED_DAYS
        left = lo + int(_unit("short-days", seed, delivery_id, sku) * (hi - lo + 1))
        return LineExpiry(day + left, batch, short_dated=True)
    life = SHELF_LIFE_DAYS.get(sku, DEFAULT_SHELF_LIFE)
    share = LEFT_MIN + (LEFT_MAX - LEFT_MIN) * _unit("left", seed, delivery_id, sku)
    return LineExpiry(day + max(7, round(life * share)), batch)


_SEEDED: dict[tuple[int, int, int, int], dict[tuple[str, str], int]] = {}


def seeded_short_dated(world: World, n: int = SEEDED_LINES) -> dict[tuple[str, str], int]:
    """Cached per world (seed, live day, deliveries, n): see ``_seeded``."""
    key = (world.seed, world.live_day, len(world.deliveries), n)
    if key not in _SEEDED:
        _SEEDED[key] = _seeded(world, n)
    return dict(_SEEDED[key])


def _seeded(world: World, n: int) -> dict[tuple[str, str], int]:
    """(delivery id, sku) -> seeded expiry day for the demo's slowest lines.

    The lines with stock left on the live day that the estimator says will
    take longest to sell (ties by shop and SKU), one per shop, their latest
    delivery's batch set to expire 9 to 20 days after the live day."""
    from mimi.stock.estimator import estimate
    from mimi.stock.history import item_histories

    live = world.live_day
    ranked = []
    for (shop_id, sku), h in item_histories(world, upto_day=live).items():
        e = estimate(h, [], live)
        if e is None or e.days_to_run_out is None or e.estimated_units_left < 3:
            continue
        ranked.append((-e.days_to_run_out, shop_id, sku, h.stock_in[-1].delivery_id))
    ranked.sort()
    out: dict[tuple[str, str], int] = {}
    shops: set[str] = set()
    for _neg, shop_id, sku, delivery_id in ranked:
        if shop_id in shops:
            continue
        # Half close to expiry (a move or a promo), half with time left to
        # collect and resell (a pickup is still an option).
        lo, hi = SEEDED_DAYS if len(out) % 2 == 0 else SEEDED_RESELL_DAYS
        out[(delivery_id, sku)] = live + lo + int(_unit("seeded", world.seed, delivery_id, sku)
                                                  * (hi - lo + 1))
        shops.add(shop_id)
        if len(out) >= n:
            break
    return out


def expiry_map(world: World, *, seed_demo: bool = True
               ) -> dict[tuple[str, str], LineExpiry]:
    """(delivery id, sku) -> expiry for every delivered line, live day included."""
    seeded = seeded_short_dated(world) if seed_demo else {}
    out: dict[tuple[str, str], LineExpiry] = {}
    for d in (*world.deliveries, *world.live_deliveries):
        for ln in d.lines:
            e = line_expiry(world.seed, d.delivery_id, ln.sku, d.day)
            day = seeded.get((d.delivery_id, ln.sku))
            if day is not None:
                e = LineExpiry(day, e.batch, short_dated=True, seeded=True)
            out[(d.delivery_id, ln.sku)] = e
    return out



DEMO_COUNT_LAG = 2


def demo_counts(world: World) -> list[dict[str, Any]]:
    """The demo's check-in counts (``stock_counts`` rows, source ``checkin``):
    each seeded slow line counted by its shop two days before the live day, at
    what the estimate says is left then (so the count agrees with the
    deliveries)."""
    from mimi.stock.estimator import estimate
    from mimi.stock.history import item_histories

    day = world.live_day - DEMO_COUNT_LAG
    hs = item_histories(world, upto_day=day)
    out = []
    for delivery_id, sku in sorted(seeded_short_dated(world)):
        shop_id = world.delivery(delivery_id).shop_id
        h = hs.get((shop_id, sku))
        e = estimate(h, [], day) if h is not None else None
        if h is None or e is None:
            continue
        out.append({"shop_id": shop_id, "sku": sku, "item": h.name,
                    "quantity": e.estimated_units_left,
                    "counted_on": world.date_of(day).isoformat(), "source": "checkin"})
    return out
