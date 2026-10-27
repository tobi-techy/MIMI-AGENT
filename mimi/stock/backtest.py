"""Backtest of estimator v0.2 with the one-tap loop (RAI-270). No model calls.

    python -m mimi.stock.backtest            # both histories, both seeds; compare to the report
    python -m mimi.stock.backtest --write    # write eval/stock/backtest.report.json
    python -m mimi.stock.backtest --strict   # also fail when the proposed bar is not met

Each history is played day by day through the production path: every
morning (09:00 WAT, days 14 to 55) ``StockDesk.morning_check`` estimates
every item and opens a job per question (trigger rule and weekly caps),
``StockFlow`` asks on a real ``JobRunner``, the simulated shop taps the
bucket of what is truly on its shelf, and the flow records the count,
re-estimates and drafts a restock. Answers are taps only here (no model):
the answer reader has its own eval (``mimi.stock.eval``).

Scored on weeks 5 to 8 (days 28 to 55), on every morning's estimate after
that morning's count:

* **run-out within 2 days** (the RAI-270 bar, proposed: >= 70%): over every
  item-day whose true shelf, with nothing more delivered, empties within 7
  days (the restock planning week); also within 3 and 14 days, and by
  confidence;
* the "likely to run low" flag (estimated under 3 days) against the truth:
  precision and recall;
* questions per item per week (cap 1) and per shop per week (cap 3), drafts
  without a count (0), orders placed (0).

Histories: ``reorders`` (``reorders.py``: shops reorder when low; the
estimator's model) on the dev seed 2610, which v0 was tuned on, and the
held-out seed 7, which it never saw (the pass mark is read on the held-out
seed); and ``route`` (``truth.py``: the RAI-260 world's own random-basket
deliveries, which carry no reorder rhythm) as a stress test without a mark.
Baselines: the same estimator never asking (no counts), and a naive rule
(units left = the last delivery minus 28-day units in per day).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from mimi.models.fake import FakeModel
from mimi.runtime.runner import JobRunner
from mimi.runtime.store import InMemoryJobStore
from mimi.runtime.tracing import RecordingStepTracer
from mimi.stock.answers import StockAnswerReader, fake_answer
from mimi.stock.estimator import POLICY_VERSION, estimate
from mimi.stock.flow import STOCK_HELPER, StockDesk, StockFlow, StockLedger
from mimi.stock.history import ItemHistory, bucket_of
from mimi.stock.intents import STOCK_INTENTS
from mimi.stock.reorders import DEV_SEED, HELD_OUT_SEED, generate_reorders
from mimi.stock.trigger import SHOP_WEEKLY_CAP, iso_week
from mimi.stock.truth import ItemTruth, generate_truth
from mimi.world import load_world
from mimi.world.model import World

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "eval" / "stock" / "backtest.report.json"
WAT = timezone(timedelta(hours=1), "WAT")
FIRST_DAY, SCORE_FROM, LAST_DAY = 14, 28, 55
PLAN_DAYS = 7
PASS_MARKS: dict[str, float] = {
    "run_out_within_2_days": 0.70,  # proposed in RAI-270, read on the held-out seed
    "max_questions_per_item_week": 1,
    "max_questions_per_shop_week": SHOP_WEEKLY_CAP,
    "drafts_without_count": 0,
    "orders_placed": 0,
}
MAX_DROP = 0.02
# Extra seeds, never used for tuning: a spread, not a mark. Run after v0.2
# was chosen on the dev seed and read once on the held-out seed.
ROBUSTNESS_SEEDS = (11, 42, 99, 123, 2027, 31337)
Key = tuple[str, str]


@dataclass
class Sink:
    accepted: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)

    def submit(self, tenant_id: str, key: str, kind: str, payload: dict[str, Any]) -> str:
        self.accepted.setdefault(key, (kind, payload))
        return key


def morning(world: World, day: int) -> datetime:
    return datetime.combine(world.date_of(day), time(9, 0), WAT)


def play(
    world: World, histories: dict[Key, ItemHistory], truth: dict[Key, ItemTruth], *, ask: bool = True
) -> dict[str, Any]:
    """Run the loop; return every scored estimate and what the flow did."""
    start = morning(world, 0)
    clock = {"now": start}
    store = InMemoryJobStore()
    ledger = StockLedger()
    desk = StockDesk(store, ledger, world.tenant_id, lambda day: histories,
                     lambda now: (now.date() - start.date()).days)
    flow = StockFlow(ledger, lambda day: histories, {s.sku: s for s in world.skus},
                     lambda shop_id: world.shop(shop_id).restock_days,
                     StockAnswerReader(FakeModel(fake_answer, name="fake:stock")),
                     world.distributor, world.price_rise_day)
    sink = Sink()
    runner = JobRunner(store=store, sink=sink, tracer=RecordingStepTracer(),
                       steps={STOCK_HELPER: flow}, worker_id="backtest",
                       clock=lambda: clock["now"], intent_types=STOCK_INTENTS)
    scored: list[dict[str, Any]] = []
    asks: list[tuple[str, str, str]] = []
    n = 0
    for day in range(FIRST_DAY, LAST_DAY + 1):
        now = morning(world, day)
        clock["now"] = now
        jobs = desk.morning_check(now) if ask else []
        while runner.run_once() is not None:
            pass
        for job_id in jobs:
            raw = ledger.asked_on[job_id]["estimate"]
            key = (raw["shop_id"], raw["sku"])
            asks.append((key[0], key[1], iso_week(now.date())))
            n += 1
            tapped = bucket_of(truth[key].counted(day))
            clock["now"] = now + timedelta(minutes=20)
            desk.receive(f"wamid.bt-{n}", key[0], tapped, clock["now"])
            clock["now"] += timedelta(minutes=3)  # past the burst settle
            while runner.run_once() is not None:
                pass
        if day < SCORE_FROM:
            continue
        for key, h in histories.items():
            e = estimate(h, ledger.counts_for(key), day)
            t = truth[key]
            if e is None:
                continue
            true_out = t.run_out_day(day)
            scored.append({"key": key, "day": day, "pred": e.run_out_day, "true": true_out,
                           "conf": e.confidence, "days": e.days_to_run_out,
                           "naive": _naive(h, day)})
    kinds = Counter(kind for kind, _ in sink.accepted.values())
    drafts = [p for k, p in sink.accepted.values() if k == "restock_draft_for_approval"]
    counted = {(p["scope"]["shop_id"], p["sku"]) for k, p in sink.accepted.values()
               if k == "record_stock_count"}
    return {"scored": scored, "asks": asks, "kinds": dict(kinds), "drafts": drafts,
            "drafts_without_count": sum(1 for d in drafts
                                        if (d["scope"]["shop_id"], d["sku"]) not in counted),
            "orders_placed": sum(1 for d in drafts if d.get("places_order") or d["status"] != "draft"),
            "signals": [p for k, p in sink.accepted.values() if k == "low_stock_signal"]}


def _naive(h: ItemHistory, day: int) -> int | None:
    seen = [s for s in h.stock_in if s.day < day]
    if not seen:
        return None
    rate = sum(s.quantity for s in seen if s.day >= day - 28) / 28
    if rate <= 0:
        return None
    left = max(0.0, seen[-1].quantity - rate * (day - seen[-1].day))
    return day + max(0, math.ceil(left / rate) - 1)


def _within(rows: list[dict[str, Any]], field_: str = "pred") -> float:
    ok = [r for r in rows if r[field_] is not None and r["true"] is not None]
    if not rows:
        return 0.0
    return round(sum(abs(r[field_] - r["true"]) <= 2 for r in ok) / len(rows), 4)


def score(run: dict[str, Any], no_counts: dict[str, Any]) -> dict[str, Any]:
    rows = run["scored"]

    def horizon(rs: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
        return [r for r in rs if r["true"] is not None and r["true"] - r["day"] < days]

    plan = horizon(rows, PLAN_DAYS)
    per_item_week = Counter(run["asks"])
    per_shop_week = Counter((s, w) for s, _, w in run["asks"])
    flagged = [r for r in rows if r["days"] is not None and r["days"] < 3]
    truly_low = horizon(rows, 3)
    return {
        "items": len({r["key"] for r in rows}),
        "item_days_scored": len(rows),
        "item_days_true_run_out_within_7": len(plan),
        "run_out_within_2_days": _within(plan),
        "run_out_within_2_days_by_horizon": {
            "within_3_days": _within(horizon(rows, 3)),
            "within_7_days": _within(plan),
            "within_14_days": _within(horizon(rows, 14)),
        },
        "run_out_within_2_days_by_confidence": {
            c: {"share": _within([r for r in plan if r["conf"] == c]),
                "n": sum(1 for r in plan if r["conf"] == c)}
            for c in ("low", "medium", "high")
        },
        "likely_low_flag_precision": round(
            sum(1 for r in flagged if r["true"] is not None and r["true"] - r["day"] < 3)
            / max(1, len(flagged)), 4),
        "likely_low_flag_recall": round(
            sum(1 for r in truly_low if r["days"] is not None and r["days"] < 3)
            / max(1, len(truly_low)), 4),
        "questions": len(run["asks"]),
        "questions_per_item_week": dict(Counter(per_item_week.values())),
        "max_questions_per_item_week": max(per_item_week.values(), default=0),
        "max_questions_per_shop_week": max(per_shop_week.values(), default=0),
        "intents": run["kinds"],
        "restock_drafts": len(run["drafts"]),
        "drafts_without_count": run["drafts_without_count"],
        "orders_placed": run["orders_placed"],
        "signals_with_counts": sum(
            1 for s in run["signals"] if any(k in s for k in ("units", "count", "units_left"))),
        "baseline_no_counts": _within(horizon(no_counts["scored"], PLAN_DAYS)),
        "baseline_naive": _within(plan, "naive"),
    }


def run_all() -> dict[str, Any]:
    world = load_world()
    out: dict[str, Any] = {"policy_version": POLICY_VERSION, "plan_days": PLAN_DAYS,
                           "scored_days": [SCORE_FROM, LAST_DAY], "histories": {}}
    for name, seed in (("reorders_dev", DEV_SEED), ("reorders_held_out", HELD_OUT_SEED)):
        rw = generate_reorders(world, seed)
        out["histories"][name] = {"seed": seed, **score(
            play(world, rw.histories, rw.truth), play(world, rw.histories, rw.truth, ask=False))}
    from mimi.stock.history import item_histories

    hist = item_histories(world)
    tr = generate_truth(world, DEV_SEED)
    out["histories"]["route_stress"] = {"seed": DEV_SEED, **score(
        play(world, hist, tr.items), play(world, hist, tr.items, ask=False))}
    spread: dict[str, float] = {}
    for seed in ROBUSTNESS_SEEDS:
        rw = generate_reorders(world, seed)
        run = play(world, rw.histories, rw.truth)
        plan = [r for r in run["scored"] if r["true"] is not None and r["true"] - r["day"] < PLAN_DAYS]
        spread[str(seed)] = _within(plan)
    vals = list(spread.values())
    out["robustness"] = {"seeds": spread, "mean": round(sum(vals) / len(vals), 4),
                         "min": min(vals), "max": max(vals),
                         "at_or_over_bar": sum(v >= PASS_MARKS["run_out_within_2_days"] for v in vals)}
    held = out["histories"]["reorders_held_out"]
    safety = {k: max(h[k] for h in out["histories"].values())
              for k in ("max_questions_per_item_week", "max_questions_per_shop_week",
                        "drafts_without_count", "orders_placed")}
    out["headline"] = {"run_out_within_2_days": held["run_out_within_2_days"], **safety}
    out["pass"] = {
        k: (out["headline"][k] >= mark if k == "run_out_within_2_days" else out["headline"][k] <= mark)
        for k, mark in PASS_MARKS.items()
    }
    out["digest"] = hashlib.sha256(json.dumps([out["histories"], out["robustness"]],
                                              sort_keys=True).encode()).hexdigest()
    return out


def _table(r: dict[str, Any]) -> str:
    h = r["histories"]
    rows = [
        ("Run-out within 2 days (true run-out within 7 days)", ">= 70% (proposed)"),
    ]
    out = ["| Metric | Mark | Reorders, held-out seed 7 | Reorders, dev seed 2610 | Route world (stress) |",
           "|---|---|---|---|---|"]
    a, b, c = h["reorders_held_out"], h["reorders_dev"], h["route_stress"]
    for label, mark in rows:
        out.append(f"| {label} | {mark} | {a['run_out_within_2_days']:.1%} | "
                   f"{b['run_out_within_2_days']:.1%} | {c['run_out_within_2_days']:.1%} |")
    for label, key in (("within 3 days", "within_3_days"), ("within 14 days", "within_14_days")):
        out.append(f"| ... true run-out {label} | | {a['run_out_within_2_days_by_horizon'][key]:.1%} | "
                   f"{b['run_out_within_2_days_by_horizon'][key]:.1%} | "
                   f"{c['run_out_within_2_days_by_horizon'][key]:.1%} |")
    for conf in ("high", "medium", "low"):
        f = [x["run_out_within_2_days_by_confidence"][conf] for x in (a, b, c)]
        out.append(f"| ... confidence {conf} | | {f[0]['share']:.1%} (n={f[0]['n']}) | "
                   f"{f[1]['share']:.1%} (n={f[1]['n']}) | {f[2]['share']:.1%} (n={f[2]['n']}) |")
    for label, key in (("Baseline: same estimator, no counts", "baseline_no_counts"),
                       ("Baseline: naive (last delivery, 28-day rate)", "baseline_naive"),
                       ("'Likely low' flag precision", "likely_low_flag_precision"),
                       ("'Likely low' flag recall", "likely_low_flag_recall")):
        out.append(f"| {label} | | {a[key]:.1%} | {b[key]:.1%} | {c[key]:.1%} |")
    for label, key, mark in (("Questions asked", "questions", ""),
                             ("Max questions per item per week", "max_questions_per_item_week", "<= 1"),
                             ("Max questions per shop per week", "max_questions_per_shop_week", "<= 3"),
                             ("Restock drafts", "restock_drafts", ""),
                             ("Drafts without the shop's count", "drafts_without_count", "0"),
                             ("Orders placed", "orders_placed", "0")):
        out.append(f"| {label} | {mark} | {a[key]} | {b[key]} | {c[key]} |")
    rb = r["robustness"]
    out.append("")
    out.append(f"Spread over {len(rb['seeds'])} more unseen reorder seeds (no mark): mean {rb['mean']:.1%}, "
               f"min {rb['min']:.1%}, max {rb['max']:.1%}, {rb['at_or_over_bar']} of {len(rb['seeds'])} at or over 70%")
    out.append("")
    out.append("pass: " + json.dumps(r["pass"]))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.stock.backtest")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    r = run_all()
    print(_table(r))
    ok = all(v for k, v in r["pass"].items() if k != "run_out_within_2_days")
    if not r["pass"]["run_out_within_2_days"]:
        print(f"NOTE: run-out within 2 days is {r['headline']['run_out_within_2_days']:.1%} on the "
              "held-out seed, under the proposed 70% bar (see docs/STOCK.md)")
        if args.strict:
            ok = False
    if args.write:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(r, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {REPORT.relative_to(ROOT)}")
    elif REPORT.exists():
        committed = json.loads(REPORT.read_text(encoding="utf-8"))
        if committed.get("digest") != r["digest"]:
            drop = committed["headline"]["run_out_within_2_days"] - r["headline"]["run_out_within_2_days"]
            print(f"note: differs from the committed report (headline change {-drop:+.1%})")
            if drop > MAX_DROP:
                print("FAIL: run-out accuracy dropped more than 2 points from the committed report")
                ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
