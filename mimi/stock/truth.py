"""The shelf truth: what each shop really sold. Sim and eval only.

Mimi never sees this, exactly like a delivery's ``Shortage``: it is the
ground truth the backtest scores the estimator against and the simulated
shops answer counts from. It is generated in code from a seed, separate from
the world's seed, so the estimator can be tuned on one truth (the dev seed)
and reported on another it never saw (the held-out seed).

The model, fixed before the estimator was written (docs/STOCK.md):

* Stock in is the world's own deliveries (the rep's van route), exact.
* Each (shop, SKU) has a hidden mean demand: what was delivered over the 8
  weeks divided by 56 days, times a per-item factor drawn from 0.7 to 1.2
  (some lines sell through, some pile up).
* Daily demand varies: Sunday 0.6x, Saturday 1.2x, Ado market day 1.4x and the
  day before it 1.15x, month end (25th to 2nd) 1.15x, and day-to-day noise
  (gamma, coefficient of variation 0.5).
* Stock on the first day is 0 to 1x the shop's usual delivered quantity.
* Sales stop at zero stock (a stock-out loses the sale).

Quantities here are floats (a shop sells packets out of a carton); every
count a simulated shop gives is a whole number, rounded down.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

from mimi.stock.history import ItemHistory, item_histories
from mimi.world.generator import is_market_day, is_month_end
from mimi.world.model import World

DEV_SEED = 2610
HELD_OUT_SEED = 7
HORIZON_DAYS = 84  # 8 weeks of history, the live day, 4 weeks beyond
DEMAND_FACTOR = (0.7, 1.2)
DAILY_CV = 0.5


def _rng(*parts: object) -> random.Random:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def day_factor(world: World, day: int) -> float:
    weekday = (date.fromisoformat(world.start_date) + timedelta(days=day)).weekday()
    f = {6: 0.6, 5: 1.2}.get(weekday, 1.0)
    if is_market_day(day, world.market_day_offset):
        f *= 1.4
    elif is_market_day(day + 1, world.market_day_offset):
        f *= 1.15
    if is_month_end(day):
        f *= 1.15
    return f


@dataclass(frozen=True)
class ItemTruth:
    shop_id: str
    sku: str
    mean_demand: float  # units a day, before the day factors
    # stock at the start of each day 0..HORIZON_DAYS, and units sold each day,
    # with the world's deliveries as the only stock in.
    start_stock: tuple[float, ...]
    sold: tuple[float, ...]
    demand: tuple[float, ...]

    def stock_at(self, day: int) -> float:
        """Stock at the start of ``day`` (before that day's delivery)."""
        return self.start_stock[day]

    def counted(self, day: int, after_delivery: bool = False, arrived: int = 0) -> int:
        """What the shop finds if it counts on the morning of ``day``."""
        return int(self.start_stock[day] + (arrived if after_delivery else 0))

    def run_out_day(self, day: int, extra_in: float = 0.0) -> int | None:
        """The first day, from ``day`` on, that ends with the shelf empty if
        nothing more is delivered after ``day`` starts (the counterfactual
        the estimate predicts). ``None`` when it lasts past the horizon."""
        stock = self.start_stock[day] + extra_in
        for d in range(day, len(self.demand)):
            stock -= self.demand[d]
            if stock <= 1e-9:
                return d
        return None


@dataclass(frozen=True)
class ShelfTruth:
    seed: int
    items: dict[tuple[str, str], ItemTruth]

    def item(self, shop_id: str, sku: str) -> ItemTruth:
        return self.items[(shop_id, sku)]


def generate_truth(world: World, seed: int = DEV_SEED) -> ShelfTruth:
    hist = item_histories(world, include_live=True)
    history_days = world.weeks * 7
    out: dict[tuple[str, str], ItemTruth] = {}
    for key, h in hist.items():
        out[key] = _item(world, h, seed, history_days)
    return ShelfTruth(seed, out)


def _item(world: World, h: ItemHistory, seed: int, history_days: int) -> ItemTruth:
    rng = _rng("shelf", seed, h.shop_id, h.sku)
    delivered = sum(s.quantity for s in h.stock_in if s.day < history_days)
    usual = median(s.quantity for s in h.stock_in)
    mean = delivered / history_days * rng.uniform(*DEMAND_FACTOR)
    stock = usual * rng.uniform(0.0, 1.0)
    arrivals = [0.0] * (HORIZON_DAYS + 1)
    for s in h.stock_in:
        arrivals[s.day] += s.quantity
    shape = 1 / DAILY_CV**2
    starts, sold, demand = [], [], []
    for day in range(HORIZON_DAYS + 1):
        starts.append(stock)
        want = mean * day_factor(world, day) * rng.gammavariate(shape, 1 / shape)
        demand.append(want)
        stock += arrivals[day]
        take = min(stock, want)
        sold.append(take)
        stock -= take
    return ItemTruth(h.shop_id, h.sku, mean, tuple(starts), tuple(sold), tuple(demand))
