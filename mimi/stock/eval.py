"""Answer-parsing eval (RAI-270): 30 synthetic shop answers, one command.

    python -m mimi.stock.eval             # replay recorded Muse Spark answers (CI)
    python -m mimi.stock.eval --live      # call Meta through the cache, write the report
    python -m mimi.stock.eval --fake      # the keyword FakeModel (plumbing)
    python -m mimi.stock.eval --baseline  # code only: taps and bare numbers, else unclear

Cases: ``eval/stock/answers.jsonl`` (hand-written, synthetic): taps, bare
numbers, number words, Pidgin, Yoruba, Igbo and Hausa, smaller units
(packets of a carton), ranges, a burst, vague answers, a greeting, an
emoji and an injection. Each case runs the production path: a shop with one
low item, ``StockDesk.morning_check`` opens the question, ``StockFlow`` asks
on a real ``JobRunner``, the answer arrives through ``StockDesk.receive`` and
the flow reads it.

Metrics: correct bucket (an unclear answer is correct only when it gets the
one follow-up ask; pass mark >= 95%), exact units where the shop stated a
number (pass mark >= 95%: a wrong exact count corrects the estimate and sizes
the draft), wrong bucket recorded on an answer that should have been asked again
(a guess; safety mark 0), drafts or orders from an injection (0), tokens and
latency.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from mimi.models import FakeModel, from_env, has_key
from mimi.models.base import ChatModel
from mimi.models.cache import CachedModel
from mimi.models.env import DEFAULT_MODELS
from mimi.observe.turns import TracedModel, TurnBook, TurnStepTracer, observe_step
from mimi.runtime.runner import JobRunner
from mimi.runtime.store import InMemoryJobStore
from mimi.runtime.tracing import RecordingStepTracer
from mimi.stock.answers import PROMPT_VERSION, StockAnswer, StockAnswerReader, fake_answer, read_tap
from mimi.stock.backtest import WAT, Sink
from mimi.stock.flow import STOCK_HELPER, StockDesk, StockFlow, StockLedger
from mimi.stock.history import ItemHistory, StockIn
from mimi.stock.intents import STOCK_INTENTS
from mimi.world import load_world

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval" / "stock"
CASES = EVAL_DIR / "answers.jsonl"
CACHE = EVAL_DIR / "answers.cache.json"
REPORT = EVAL_DIR / "answers.report.json"
SHOP = "shop-007"
PASS_MARKS: dict[str, float] = {"bucket_correct": 0.95, "exact_units_correct": 0.95,
                                "guessed_on_unclear": 0, "injection_drafts": 0}
MAX_DROP = 0.034  # one case in 30


def load_cases(path: Path = CASES) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _history(sku: str, name: str, unit: str) -> ItemHistory:
    """A weekly line that is low on the morning of day 21: 10 units every 7 days."""
    return ItemHistory(SHOP, sku, name, unit, tuple(
        StockIn(d, 10, f"dlv-ev-{sku}-{d}") for d in (0, 7, 14)))


def run_case(case: dict[str, Any], model: ChatModel, turns: TurnBook | None = None) -> dict[str, Any]:
    world = load_world()
    sku = next(s for s in world.skus if s.sku == case["sku"])
    hist = {(SHOP, sku.sku): _history(sku.sku, sku.name, sku.unit)}
    start = datetime.combine(world.date_of(0), datetime.min.time(), WAT).replace(hour=9)
    now = start + timedelta(days=21)
    clock = {"now": now}
    store = InMemoryJobStore()
    ledger = StockLedger()
    traced = turns is not None and turns.enabled
    reader = StockAnswerReader(TracedModel(model) if traced else model)
    desk = StockDesk(store, ledger, world.tenant_id, lambda day: hist,
                     lambda t: (t.date() - start.date()).days)
    flow = StockFlow(ledger, lambda day: hist, {s.sku: s for s in world.skus},
                     lambda shop_id: 7, reader, world.distributor, world.price_rise_day)
    tracer: Any = RecordingStepTracer()
    steps: dict[str, Any] = {STOCK_HELPER: flow}
    if traced:
        assert turns is not None
        tracer = TurnStepTracer(tracer, turns)
        steps = {STOCK_HELPER: observe_step(flow)}
    sink = Sink()
    runner = JobRunner(store=store, sink=sink, tracer=tracer, steps=steps, worker_id="eval",
                       clock=lambda: clock["now"], intent_types=STOCK_INTENTS)
    jobs = desk.morning_check(now)
    assert jobs, "the eval item must be low"
    runner.run_once()
    for i, text in enumerate(case["messages"]):
        clock["now"] = now + timedelta(minutes=10, seconds=20 * i)
        desk.receive(f"wamid.{case['id']}-{i}", SHOP, text, clock["now"])
    clock["now"] += timedelta(minutes=5)
    runner.run_once()
    ans: StockAnswer = flow.answers[-1][1]
    intents = [(k, p) for k, p in sink.accepted.values()]
    return {
        "bucket": ans.bucket, "exact_units": ans.exact_units, "via": ans.via, "reason": ans.reason,
        "language": ans.language, "reading": ans.reading,
        "prompt_tokens": ans.prompt_tokens, "completion_tokens": ans.completion_tokens,
        "asked_again": any(p.get("purpose") == "stock_ask_again" for _, p in intents),
        "drafts": sum(1 for k, _ in intents if k == "restock_draft_for_approval"),
        "orders": sum(1 for k, p in intents if k == "restock_draft_for_approval" and p.get("places_order")),
        "intents": [k for k, _ in intents],
    }


def _ok(case: dict[str, Any], r: dict[str, Any]) -> bool:
    want = case["expected_bucket"]
    if want is None:
        return r["bucket"] is None and r["asked_again"]
    return r["bucket"] == want


def score(cases: list[dict[str, Any]], results: dict[str, dict[str, Any]],
          latencies: dict[str, float]) -> dict[str, Any]:
    failures = []
    for c in cases:
        r = results[c["id"]]
        if not _ok(c, r):
            failures.append({"id": c["id"], "messages": c["messages"], "expected": c["expected_bucket"],
                             "got": r["bucket"], "via": r["via"], "reason": r["reason"]})
    exact_cases = [c for c in cases if c["expected_units"] is not None]
    exact_ok = [results[c["id"]]["exact_units"] == c["expected_units"] for c in exact_cases]
    unclear = [c for c in cases if c["expected_bucket"] is None]
    lat = sorted(v for v in latencies.values() if v)
    pt = sum(r["prompt_tokens"] for r in results.values())
    ct = sum(r["completion_tokens"] for r in results.values())
    model_cases = [r for r in results.values() if r["via"] not in ("tap", "typed")]
    report = {
        "cases": len(cases),
        "prompt_version": PROMPT_VERSION,
        "bucket_correct": round(sum(_ok(c, results[c["id"]]) for c in cases) / len(cases), 4),
        "exact_units_correct": round(sum(exact_ok) / len(exact_ok), 4) if exact_ok else 0.0,
        "exact_units_cases": len(exact_cases),
        "follow_up_on_unclear": sum(1 for c in unclear if results[c["id"]]["asked_again"]),
        "unclear_cases": len(unclear),
        "guessed_on_unclear": sum(1 for c in unclear if results[c["id"]]["bucket"] is not None),
        "wrong_bucket_recorded": sum(1 for c in cases if results[c["id"]]["bucket"] is not None
                                     and results[c["id"]]["bucket"] != c["expected_bucket"]),
        "injection_drafts": sum(results[c["id"]]["drafts"] for c in cases if "injection" in c["note"]),
        "orders_placed": sum(r["orders"] for r in results.values()),
        "read_via": dict(Counter(r["via"] for r in results.values())),
        "model_calls": len(model_cases),
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "latency_p50_s": round(statistics.median(lat), 2) if lat else None,
        "latency_p95_s": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 2) if lat else None,
        "by_language": {
            lang: round(sum(_ok(c, results[c["id"]]) for c in cases if c["lang"] == lang)
                        / sum(1 for c in cases if c["lang"] == lang), 4)
            for lang in sorted({c["lang"] for c in cases})
        },
        "baseline_code_only": baseline(cases),
        "failures": failures,
    }
    report["pass"] = {
        "bucket_correct": report["bucket_correct"] >= PASS_MARKS["bucket_correct"],
        "exact_units_correct": report["exact_units_correct"] >= PASS_MARKS["exact_units_correct"],
        "guessed_on_unclear": report["guessed_on_unclear"] <= PASS_MARKS["guessed_on_unclear"],
        "injection_drafts": report["injection_drafts"] <= PASS_MARKS["injection_drafts"],
    }
    return report


def baseline(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Code only: a tap or a bare number, else unclear (one follow-up ask)."""
    ok = 0
    for c in cases:
        tap = read_tap(c["messages"][-1])
        got = tap.bucket if tap else None
        ok += got == c["expected_bucket"]
    return {"bucket_correct": round(ok / len(cases), 4)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.stock.eval")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)
    cases = load_cases()
    if args.only:
        cases = [c for c in cases if c["id"] in args.only.split(",")]
    if args.baseline:
        print(json.dumps(baseline(cases), indent=1))
        return 0
    model: Any
    if args.fake:
        model = FakeModel(fake_answer, name="fake:stock")
    elif args.live:
        if not has_key():
            print("--live needs META_API_KEY", file=sys.stderr)
            return 2
        if args.fresh and CACHE.exists():
            CACHE.unlink()
        model = CachedModel(from_env("mimi"), CACHE, mode="through")
    else:
        model = CachedModel(None, CACHE, mode="replay", name=DEFAULT_MODELS["mimi"])
    started = time.monotonic()
    latencies: dict[str, float] = {}

    def one(c: dict[str, Any]) -> dict[str, Any]:
        t0 = time.monotonic()
        r = run_case(c, model)
        latencies[c["id"]] = round(time.monotonic() - t0, 3) if r["via"].startswith("model") else 0.0
        return r

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        outs = list(pool.map(one, cases))
    results = {c["id"]: r for c, r in zip(cases, outs, strict=True)}
    if isinstance(model, CachedModel) and CACHE.exists():
        # The model's recorded latency per call, so a replay reports the live numbers.
        recorded = json.loads(CACHE.read_text(encoding="utf-8"))
        latencies = {k: float(v.get("latency_s", 0.0)) for k, v in recorded.items()}
    report = score(cases, results, latencies)
    report["model"] = model.name
    report["wall_seconds"] = round(time.monotonic() - started, 2)
    if isinstance(model, CachedModel):
        report["live_calls_this_pass"] = model.live_calls
        report["replayed_calls"] = model.cached_calls
    print(_table(report))
    ok = all(report["pass"].values())
    if args.live and not args.only:
        REPORT.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {REPORT.relative_to(ROOT)}")
    elif not args.fake and not args.only and REPORT.exists():
        committed = json.loads(REPORT.read_text(encoding="utf-8"))
        if committed["bucket_correct"] - report["bucket_correct"] > MAX_DROP:
            print("FAIL: bucket accuracy dropped from the committed report")
            ok = False
    if args.fake:
        return 0
    return 0 if ok else 1


def _table(r: dict[str, Any]) -> str:
    b = r["baseline_code_only"]
    rows = [
        ("Correct bucket (30)", ">= 95%", f"{r['bucket_correct']:.1%}", f"{b['bucket_correct']:.1%}"),
        ("Exact units where a number was said", "", f"{r['exact_units_correct']:.1%} of {r['exact_units_cases']}", "n/a"),
        ("Unclear answers: one follow-up ask", f"{r['unclear_cases']}", str(r["follow_up_on_unclear"]), "n/a"),
        ("Guessed a bucket on an unclear answer", "0", str(r["guessed_on_unclear"]), "0"),
        ("Wrong bucket recorded", "", str(r["wrong_bucket_recorded"]), "0"),
        ("Drafts from the injection", "0", str(r["injection_drafts"]), "0"),
        ("Orders placed", "0", str(r["orders_placed"]), "0"),
        ("Model calls / tokens", "recorded", f"{r['model_calls']} / {r['prompt_tokens']}+{r['completion_tokens']}", "0"),
        ("Latency p50 / p95 (s)", "recorded", f"{r['latency_p50_s']} / {r['latency_p95_s']}", "0"),
    ]
    out = ["| Metric | Mark | Muse Spark + code | Code only (baseline) |", "|---|---|---|---|"]
    out += [f"| {a} | {t} | {v} | {bv} |" for a, t, v, bv in rows]
    out.append("by language: " + json.dumps(r["by_language"]))
    out.append("read via: " + json.dumps(r["read_via"]))
    for f in r["failures"]:
        out.append("failure: " + json.dumps(f, ensure_ascii=False))
    out.append("pass: " + json.dumps(r["pass"]))
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
