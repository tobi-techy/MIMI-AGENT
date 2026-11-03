"""Offline eval of the slow-stock and near-expiry alert (``slow.py``). No model.

    python -m mimi.stock.slow_eval            # dev and held-out seeds; prints the table
    python -m mimi.stock.slow_eval --write    # also writes eval/stock/slow.report.json
    python -m mimi.stock.slow_eval --check    # fails when a rerun differs from the report

A shelf-truth world built for this eval, on the 40 Coral Lane shops (size,
van cadence), their SKUs and the Ado market calendar, run day by day with
batches sold oldest first:

* hidden demand per line by shop size (``reorders.DEMAND``), drifting a few
  per cent a week, with the world's day factors and gamma noise (CV 0.5);
* the shop reorders at each van visit to cover until the next visit plus 1 to
  3 days, from its own rough sense of recent sales;
* a **slump** on 30% of lines: from a day between 28 and 50, demand falls to
  10% to 50% of what it was (a product that stopped selling). The shop orders
  less, then nothing, and the shelf sits;
* a **push** on 10% of lines: one van visit between days 35 and 50 drops 2 to
  3 times what the shop asked for;
* every delivery has a batch with an expiry: the SKU's shelf life
  (``mimi.world.expiry``) times the share left (35% to 90%), and 15% of
  batches short-dated (14 to 35 days left), more than the demo world so there
  are enough near-expiry cases to score;
* a unit still on the shelf ``SAFETY_DAYS`` before its batch expires is lost.

Mimi sees the deliveries (with batch expiry) and, like the weekly check-in,
some counts: a count the day before the live day on half the lines (40% typed
exact, the rest a 0-2 / 3-5 / 6+ tap) and one a week earlier on 30%.

**Truth** on the live day (56): positive when units on the shelf that day are
lost to expiry within the next 60 days (simulated forward with the true
demand), or the shelf holds more than ``OVERSTOCK_FACTOR`` times the line's
true normal cover (and 14 days more). **Prediction**: the alert Mimi would
send, ``assess`` gated as the API gates it (confidence medium or high).

The rules were written before this eval ran and read on the dev seed 2610;
the held-out seed 7 is reported as it comes, not tuned on.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mimi.stock.estimator import estimate
from mimi.stock.history import ItemHistory, StockCount, StockIn, assumed_units, bucket_of
from mimi.stock.reorders import DEMAND, DEV_SEED, HELD_OUT_SEED, LINES
from mimi.stock.selling import alerts
from mimi.stock.slow import (
    EXPIRY_HORIZON_DAYS,
    OVERSTOCK_EXTRA_DAYS,
    OVERSTOCK_FACTOR,
    SAFETY_DAYS,
    SlowAssessment,
    assess,
)
from mimi.stock.truth import DAILY_CV, day_factor
from mimi.world import catalogue as cat
from mimi.world.expiry import DEFAULT_SHELF_LIFE, SHELF_LIFE_DAYS
from mimi.world.loader import load_world
from mimi.world.model import World

LIVE_DAY = 56
FORWARD_DAYS = EXPIRY_HORIZON_DAYS
SLUMP_SHARE = 0.30
PUSH_SHARE = 0.10
SHORT_SHARE = 0.15
COUNT_SHARE = 0.5
EARLY_COUNT_SHARE = 0.3
TYPED_SHARE = 0.4
EXTRA_SEEDS = (11, 23, 31)
REPORT = Path(__file__).resolve().parents[2] / "eval" / "stock" / "slow.report.json"


def _rng(*parts: object) -> random.Random:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def alerted(a: SlowAssessment | None) -> bool:
    """The gate the API and the signal use (``selling.alerts``)."""
    return alerts(a)


@dataclass(frozen=True)
class Case:
    shop_id: str
    sku: str
    slump: bool
    push: bool
    counted: bool
    truth: bool
    truth_kind: str
    predicted: bool
    predicted_kind: str
    confidence: str


def _line(world: World, shop_id: str, size: str, sku: str, is_bulk: bool, cycle: int,
          visits: set[int], seed: int) -> tuple[ItemHistory, list[StockCount], bool, bool, str]:
    rng = _rng("slow-line", seed, shop_id, sku)
    mean = rng.uniform(*DEMAND[size]) * (0.4 if is_bulk else 1.0)
    slump_day = rng.randint(28, 50) if rng.random() < SLUMP_SHARE else None
    slump_to = rng.uniform(0.1, 0.5)
    push_day = None
    if rng.random() < PUSH_SHARE:
        later = sorted(v for v in visits if 35 <= v <= 50)
        push_day = rng.choice(later) if later else None
    life = SHELF_LIFE_DAYS.get(sku, DEFAULT_SHELF_LIFE)
    shape = 1 / DAILY_CV**2
    batches: list[list[float]] = []  # [units, expires_day]
    first_qty = math.ceil(mean * cycle * rng.uniform(0.6, 1.2))
    batches.append([float(first_qty), life * rng.uniform(0.35, 0.9)])
    stock_in: list[StockIn] = []
    recent: list[float] = []
    n = 0
    stock_at_live: list[list[float]] = []
    mean_at_live = mean
    live_units = 0.0
    lost_after_live = 0.0
    for day in range(LIVE_DAY + FORWARD_DAYS + 1):
        if day % 7 == 0 and day:
            mean *= math.exp(rng.gauss(0.0, 0.08))
        if slump_day is not None and day == slump_day:
            mean *= slump_to
        # Units past their last selling day are lost (pulled from the shelf).
        kept = []
        for b in batches:
            if day >= b[1] - SAFETY_DAYS and b[0] > 0:
                if day >= LIVE_DAY and any(b is x for x in stock_at_live):
                    # Whole units, per batch (as ``units_at_risk`` counts them).
                    lost_after_live += math.floor(b[0] + 1e-9)
                continue
            kept.append(b)
        batches = kept
        stock = sum(b[0] for b in batches)
        if day == LIVE_DAY:
            stock_at_live = batches[:]
            live_units = stock
            mean_at_live = mean
        if day in visits and day < LIVE_DAY:
            sense = (sum(recent[-14:]) / max(1, len(recent[-14:]))) if recent else mean
            need = sense * rng.uniform(0.85, 1.15) * (cycle + rng.uniform(1.0, 3.0))
            qty = math.ceil(need - stock) if stock < need else 0
            if push_day == day:
                qty = max(qty, 1) * rng.randint(2, 3) + math.ceil(mean * cycle)
            if qty > 0:
                n += 1
                if rng.random() < SHORT_SHARE:
                    left = rng.randint(14, 35)
                else:
                    left = max(7, round(life * rng.uniform(0.35, 0.9)))
                batches.append([float(qty), float(day + left)])
                stock_in.append(StockIn(day, qty, f"stk-{shop_id}-{sku}-{n:03d}",
                                        expires_day=day + left, batch=f"B{n:03d}"))
        want = mean * day_factor(world, day) * rng.gammavariate(shape, 1 / shape)
        take = want
        sold = 0.0
        for b in batches:  # oldest first
            if take <= 0:
                break
            q = min(b[0], take)
            b[0] -= q
            take -= q
            sold += q
        batches = [b for b in batches if b[0] > 1e-9]
        recent.append(sold)
    normal = cycle + 1.0
    cover = live_units / mean_at_live if mean_at_live > 0 else math.inf
    over = (live_units >= 2 and cover > OVERSTOCK_FACTOR * normal
            and cover - normal >= OVERSTOCK_EXTRA_DAYS)
    expires = math.floor(lost_after_live + 1e-9) >= 1
    kind = "near_expiry" if expires else "overstock" if over else ""
    hist = ItemHistory(shop_id, sku, sku, "ctn", tuple(stock_in), visit_days=cycle)
    counts = _counts(rng, hist, live_units, seed)
    return hist, counts, slump_day is not None, push_day is not None, kind


def _counts(rng: random.Random, hist: ItemHistory, live_units: float,
            seed: int) -> list[StockCount]:
    out = []
    crng = _rng("slow-counts", seed, hist.shop_id, hist.sku)
    if crng.random() < EARLY_COUNT_SHARE:
        # A week earlier: the shelf is unknown here without a second pass, so
        # the count is of the shelf on the live day plus a week of the
        # estimate's rate (the shop's own rough count).
        e = estimate(hist, [], LIVE_DAY - 8)
        if e is not None:
            units = max(0, round(live_units + 7 * e.sell_rate_per_day * crng.uniform(0.7, 1.3)))
            out.append(StockCount(LIVE_DAY - 8, bucket_of(units), units, source="typed"))
    if crng.random() < COUNT_SHARE:
        units = max(0, round(live_units))
        if crng.random() < TYPED_SHARE:
            out.append(StockCount(LIVE_DAY - 1, bucket_of(units), units, source="typed"))
        else:
            e = estimate(hist, out, LIVE_DAY - 1)
            b = bucket_of(units)
            out.append(StockCount(LIVE_DAY - 1, b,
                                  assumed_units(b, e.estimated_units_left if e else 0)))
    del rng
    return out


def run_seed(world: World, seed: int) -> list[Case]:
    bulk = {row[0]: row[5] for row in cat.CATALOGUE}
    out: list[Case] = []
    for shop in world.shops:
        rng = _rng("slow-shop", seed, shop.shop_id)
        pool = sorted(s.sku for s in world.skus if shop.size != "small" or not bulk[s.sku])
        lines = sorted(rng.sample(pool, LINES[shop.size]))
        cycle = shop.restock_days
        phase = rng.randrange(min(cycle, 6))
        visits = {d - 1 if d % 7 == 6 else d for d in range(phase, LIVE_DAY + 1, cycle)}
        for sku in lines:
            h, counts, slump, push, kind = _line(world, shop.shop_id, shop.size, sku,
                                                 bulk[sku], cycle, visits, seed)
            e = estimate(h, counts, LIVE_DAY)
            a = assess(e, h, counts) if e is not None else None
            out.append(Case(shop.shop_id, sku, slump, push, bool(counts), bool(kind), kind,
                            alerted(a), a.kind if a is not None else "",
                            a.confidence if a is not None else ""))
    return out


def score(cases: list[Case]) -> dict[str, Any]:
    tp = sum(c.truth and c.predicted for c in cases)
    fp = sum(c.predicted and not c.truth for c in cases)
    fn = sum(c.truth and not c.predicted for c in cases)
    counted = [c for c in cases if c.counted]
    ctp = sum(c.truth and c.predicted for c in counted)
    cfn = sum(c.truth and not c.predicted for c in counted)
    cfp = sum(c.predicted and not c.truth for c in counted)
    utp = sum(c.truth and bool(c.predicted_kind) for c in cases)

    def ratio(a: int, b: int) -> float | None:
        return round(a / b, 4) if b else None

    return {
        "lines": len(cases), "positives": tp + fn, "alerts": tp + fp,
        "tp": tp, "fp": fp, "fn": fn,
        "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
        "near_expiry_positives": sum(c.truth_kind == "near_expiry" for c in cases),
        "overstock_positives": sum(c.truth_kind == "overstock" for c in cases),
        "with_a_count": {"lines": len(counted), "precision": ratio(ctp, ctp + cfp),
                         "recall": ratio(ctp, ctp + cfn)},
        "without_a_count_recall": ratio(tp - ctp, (tp - ctp) + (fn - cfn)),
        # Before the confidence gate: every assessment, low confidence too.
        "ungated": {"precision": ratio(utp, sum(bool(c.predicted_kind) for c in cases)),
                    "recall": ratio(utp, tp + fn)},
    }


def run() -> dict[str, Any]:
    world = load_world()
    out: dict[str, Any] = {"policy": "slow-stock-v0.1", "live_day": LIVE_DAY, "seeds": {}}
    for name, seed in (("dev", DEV_SEED), ("held_out", HELD_OUT_SEED)):
        out["seeds"][name] = {"seed": seed, **score(run_seed(world, seed))}
    out["spread"] = {str(s): score(run_seed(world, s)) for s in EXTRA_SEEDS}
    return out


def table(r: dict[str, Any]) -> str:
    lines = ["| Seed | Lines | Positives | Alerts | Precision | Recall | Recall with a count | "
             "Recall without | Ungated P / R |",
             "|---|---|---|---|---|---|---|---|---|"]

    def pct(x: float | None) -> str:
        return "n/a" if x is None else f"{x:.0%}"

    rows = [(f"{k} ({v['seed']})", v) for k, v in r["seeds"].items()]
    rows += [(f"extra ({k})", v) for k, v in r["spread"].items()]
    for name, v in rows:
        lines.append(f"| {name} | {v['lines']} | {v['positives']} | {v['alerts']} | "
                     f"{pct(v['precision'])} | {pct(v['recall'])} | "
                     f"{pct(v['with_a_count']['recall'])} | {pct(v['without_a_count_recall'])} | "
                     f"{pct(v['ungated']['precision'])} / {pct(v['ungated']['recall'])} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--write", action="store_true", help="write eval/stock/slow.report.json")
    ap.add_argument("--check", action="store_true",
                    help="fail when a rerun differs from eval/stock/slow.report.json")
    args = ap.parse_args(argv)
    r = run()
    print(table(r))
    if args.check:
        want = json.loads(REPORT.read_text())
        if json.loads(json.dumps(r, sort_keys=True)) != want:
            print("slow eval differs from eval/stock/slow.report.json: rerun with --write")
            return 1
        print("matches eval/stock/slow.report.json")
    if args.write:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(r, indent=2, sort_keys=True) + "\n")
        print(f"wrote {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
