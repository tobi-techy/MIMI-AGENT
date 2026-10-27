"""A reorder-driven stock history for the backtest. Sim and eval only.

RAI-294 estimates sales from the reorder rhythm ("10 cartons every 5 days is
about 2 a day"). The RAI-260 world's deliveries are a rep's random basket per
visit, which carries no rhythm, so the backtest also runs on this history:
the same 40 Coral Lane shops (size, restock cadence, payment habit) and the
same SKUs and Ado market calendar, with each shop's *regular lines* sold off
the shelf day by day and reordered the way a shop does:

* hidden demand per line (by shop size; bulk lines slower), drifting a few
  per cent a week, with the world's day factors (Sunday, Saturday, market
  day and its eve, month end) and day-to-day noise (gamma, CV 0.5);
* the rep visits every ``restock_days`` (7 or 14). At a visit the shop orders
  a line when its stock will not last until the next visit plus 1 to 3 days,
  enough to cover that, from its own rough sense of recent sales (plus or
  minus 15%); a late payer sometimes orders less;
* a line that runs out between visits is reordered on WhatsApp 60% of the
  time and arrives 1 or 2 days later; otherwise the shop waits for the van;
* now and then (4% of line-weeks) the shop buys 1 to 3 units somewhere else,
  which Mimi never sees.

Mimi sees only the deliveries (and its own counts); the shelf is the truth.
Two seeds: the dev seed the estimator was tuned on, and a held-out seed it
never saw. Quantities on the shelf are floats; every delivery and every
count is a whole number.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass

from mimi.stock.history import ItemHistory, StockIn
from mimi.stock.truth import DAILY_CV, HORIZON_DAYS, ItemTruth, day_factor
from mimi.world import catalogue as cat
from mimi.world.model import World

DEV_SEED = 2610
HELD_OUT_SEED = 7
LINES = {"big": 8, "medium": 6, "small": 4}
DEMAND = {"big": (0.8, 3.0), "medium": (0.4, 1.5), "small": (0.15, 0.6)}
BULK_FACTOR = 0.4
WHATSAPP_REORDER = 0.6
ELSEWHERE_WEEKLY = 0.04


def _rng(*parts: object) -> random.Random:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


@dataclass(frozen=True)
class ReorderWorld:
    seed: int
    histories: dict[tuple[str, str], ItemHistory]
    truth: dict[tuple[str, str], ItemTruth]


def generate_reorders(world: World, seed: int = DEV_SEED) -> ReorderWorld:
    bulk = {row[0]: row[5] for row in cat.CATALOGUE}
    skus = {s.sku: s for s in world.skus}
    histories: dict[tuple[str, str], ItemHistory] = {}
    truth: dict[tuple[str, str], ItemTruth] = {}
    for shop in world.shops:
        rng = _rng("reorders", seed, shop.shop_id)
        pool = sorted(s for s in skus if shop.size != "small" or not bulk[s])
        lines = sorted(rng.sample(pool, LINES[shop.size]))
        cycle = shop.restock_days
        phase = rng.randrange(min(cycle, 6))
        visits = {d for d in range(phase, HORIZON_DAYS + 1, cycle)}
        visits = {d - 1 if (d % 7) == 6 else d for d in visits}  # no Sunday van
        for sku in lines:
            h, t = _line(world, shop.shop_id, shop.size, shop.pays, sku, bulk[sku],
                         cycle, visits, seed)
            histories[(shop.shop_id, sku)] = ItemHistory(
                shop.shop_id, sku, skus[sku].name, skus[sku].unit, h
            )
            truth[(shop.shop_id, sku)] = t
    return ReorderWorld(seed, histories, truth)


def _line(
    world: World,
    shop_id: str,
    size: str,
    pays: str,
    sku: str,
    is_bulk: bool,
    cycle: int,
    visits: set[int],
    seed: int,
) -> tuple[tuple[StockIn, ...], ItemTruth]:
    rng = _rng("line", seed, shop_id, sku)
    mean = rng.uniform(*DEMAND[size]) * (BULK_FACTOR if is_bulk else 1.0)
    shape = 1 / DAILY_CV**2
    stock = mean * cycle * rng.uniform(0.3, 1.2)
    pending: dict[int, tuple[int, int]] = {}  # arrival day -> (units, asked day)
    stock_in: list[StockIn] = []
    starts: list[float] = []
    sold: list[float] = []
    demand: list[float] = []
    recent: list[float] = []
    waiting = False
    n = 0
    for day in range(HORIZON_DAYS + 1):
        if day % 7 == 0 and day:
            mean *= math.exp(rng.gauss(0.0, 0.08))
            if rng.random() < ELSEWHERE_WEEKLY:
                stock += rng.randint(1, 3)  # bought elsewhere: Mimi never sees it
        starts.append(stock)
        arrive, asked = pending.pop(day, (0, -1))
        source = "whatsapp_reorder" if arrive else "van"
        if day in visits:
            sense = (sum(recent[-14:]) / max(1, len(recent[-14:]))) if recent else mean
            sense *= rng.uniform(0.85, 1.15)
            need = sense * (cycle + rng.uniform(1.0, 3.0))
            if stock + arrive < need:
                qty = math.ceil(need - stock - arrive)
                if pays == "late" and rng.random() < 0.3:
                    qty = math.ceil(qty * rng.uniform(0.6, 1.0))
                arrive += max(1, qty)
                source = "van"
            waiting = False
        if arrive:
            n += 1
            stock_in.append(
                StockIn(day, arrive, f"stk-{shop_id}-{sku}-{n:03d}", source,  # type: ignore[arg-type]
                        asked if source == "whatsapp_reorder" else None)
            )
            stock += arrive
        want = mean * day_factor(world, day) * rng.gammavariate(shape, 1 / shape)
        demand.append(want)
        take = min(stock, want)
        sold.append(take)
        recent.append(take)
        stock -= take
        if stock <= 1e-9 and not waiting:
            waiting = True
            if rng.random() < WHATSAPP_REORDER:
                when = day + rng.randint(1, 2)
                if (when % 7) == 6:
                    when += 1
                if when not in visits:
                    sense = sum(recent[-14:]) / max(1, len(recent[-14:]))
                    pending[when] = (max(1, math.ceil(sense * cycle)), day)
    t = ItemTruth(shop_id, sku, mean, tuple(starts), tuple(sold), tuple(demand))
    return tuple(stock_in), t
