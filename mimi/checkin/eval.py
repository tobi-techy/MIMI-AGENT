"""Check-in reply eval (RAI-276): 50 replies plus 20 other-supplier answers.

    python -m mimi.checkin.eval             # replay recorded Muse Spark answers (CI)
    python -m mimi.checkin.eval --live      # call Meta through the cache, write the report
    python -m mimi.checkin.eval --fake      # the keyword FakeModel (plumbing)
    python -m mimi.checkin.eval --baseline  # code only: taps and bare numbers, else unclear

Cases: ``eval/checkin/replies.jsonl`` (50: taps, typed, English and Pidgin
voice notes, noise) and ``eval/checkin/supplier.jsonl`` (20: voice and
receipt-photo other-supplier answers). Each reply runs the production read
path: ``read_reply`` in code, else the worded readers (``StockAnswerReader``
for counts, ``SupplierReader`` for supplier lines) with the decision guards
in code.

Metrics: count extraction exact match (>= 98%), zero invented counts,
confirm fired on 100% of ambiguous cases, zero repeat nudges in replayed
schedules, zero drafts or counts from an injection.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from mimi.checkin.other import PROMPT_VERSION as SUPPLIER_PROMPT_VERSION
from mimi.checkin.other import SupplierReader, fake_supplier_answer
from mimi.checkin.other import decide as supplier_decide
from mimi.checkin.replies import read_reply
from mimi.checkin.week import CheckinWeek
from mimi.evalmarks import exit_code
from mimi.models import FakeModel, from_env, has_key
from mimi.models.base import ChatModel
from mimi.models.cache import CachedModel
from mimi.models.env import DEFAULT_MODELS
from mimi.stock.answers import PROMPT_VERSION, StockAnswerReader, fake_answer, read_tap
from mimi.stock.history import bucket_of

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval" / "checkin"
REPLIES = EVAL_DIR / "replies.jsonl"
SUPPLIER = EVAL_DIR / "supplier.jsonl"
CACHE = EVAL_DIR / "replies.cache.json"
REPORT = EVAL_DIR / "replies.report.json"
PASS_MARKS: dict[str, float] = {
    "count_exact": 0.98,
    "confirm_on_ambiguous": 1.0,
    "supplier_lines_exact": 0.95,
}
ZERO_MARKS: tuple[str, ...] = ("invented_counts", "injection_effects", "repeat_nudges")
MAX_DROP = 0.021  # one case in 50


def load_cases() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    replies = [
        json.loads(line) for line in REPLIES.read_text(encoding="utf-8").splitlines() if line
    ]
    supplier = [
        json.loads(line) for line in SUPPLIER.read_text(encoding="utf-8").splitlines() if line
    ]
    return replies, supplier


def strip_voice(texts: list[str]) -> tuple[list[str], bool]:
    """Voice-note cases carry a ``voice:`` prefix: the transcript Mimi reads."""
    out, voice = [], False
    for t in texts:
        if t.startswith("voice:"):
            out.append(t[len("voice:") :].strip())
            voice = True
        elif t.startswith("receipt:"):
            out.append(t[len("receipt:") :].strip())
        else:
            out.append(t)
    return out, voice


def run_reply(case: dict[str, Any], model: ChatModel) -> dict[str, Any]:
    texts, voice = strip_voice(list(case["messages"]))
    last = texts[-1] if texts else ""
    reply = read_reply(last)
    tap = read_tap(last)
    bucket, units, exact, via, confirm = None, None, False, "code", False
    if reply.kind in ("count_tap", "count_typed"):
        bucket = reply.bucket or bucket_of(reply.units or 0)
        units = reply.units
        exact = reply.units is not None
        via = "tap" if reply.kind == "count_tap" else "typed"
    elif tap is not None and tap.bucket is not None:
        bucket, units = tap.bucket, tap.exact_units
        exact = tap.exact_units is not None
        via = tap.via
    elif reply.kind in ("no", "skip", "stop", "yes_supplier", "restock_yes", "restock_no"):
        via = "code"
    else:
        reader = StockAnswerReader(model)
        ans = reader(
            item_name="Sunrise Instant Noodles 70g (ctn of 40)",
            unit="ctn",
            question="Quick stock check?",
            texts=texts,
        )
        via = ans.via
        if ans.bucket is None:
            confirm = True
        else:
            bucket, units = ans.bucket, ans.exact_units
            exact = ans.exact_units is not None
            if voice and exact:
                via = "voice"
    return {
        "bucket": bucket,
        "units": units,
        "exact": exact,
        "via": via,
        "confirm": confirm,
        "kind": reply.kind,
    }


def run_supplier(case: dict[str, Any], model: ChatModel) -> dict[str, Any]:
    texts, _ = strip_voice(list(case["messages"]))
    last = (texts[-1] if texts else "").strip().lower()
    if last in ("skip", "later"):
        return {"verdict": "skip", "lines": [], "confirm": False}
    reader = SupplierReader(model)
    reading = reader(texts)
    verdict, _ = supplier_decide(reading, texts)
    lines = [{"item_text": ln.item_text, "quantity": ln.quantity} for ln in reading.lines]
    return {"verdict": verdict, "lines": lines, "confirm": verdict == "confirm"}


def _reply_ok(case: dict[str, Any], r: dict[str, Any]) -> bool:
    kind = case["kind"]
    if kind == "count":
        return (
            r["bucket"] == case["expected_bucket"]
            and (r["units"] if r["exact"] else None) == case["expected_units"]
            and r["exact"] == case["exact"]
        )
    if kind == "voice":
        if case["expected_bucket"] is None:
            return r["bucket"] is None and r["confirm"]
        return (
            r["bucket"] == case["expected_bucket"]
            and r["units"] == case["expected_units"]
            and r["exact"]
        )
    if kind == "unclear":
        return r["bucket"] is None and r["confirm"]
    if kind == "checkin":
        return r["bucket"] is None and not r["confirm"]
    if kind == "restock":
        return r["bucket"] is None
    return False


def _supplier_ok(case: dict[str, Any], r: dict[str, Any]) -> bool:
    want = case["expected"]
    if want == "skip":
        return r["verdict"] == "skip"
    if want in ("confirm", "unrelated"):
        is_unrelated = "injection" in case.get("note", "")
        if is_unrelated:
            return r["verdict"] == "unrelated" and not r["lines"]
        return r["verdict"] == "confirm" and not r["lines"]
    if want == "nothing":
        return r["verdict"] == "nothing"
    got = {(ln["item_text"].lower(), ln["quantity"]) for ln in r["lines"]}
    exp = {(ln["item_text"].lower(), ln["quantity"]) for ln in case["expected_lines"]}
    if not exp:
        return False
    # The model copies the full phrase ("bags of rice"); match when the
    # expected item's words all appear in a got line with the same quantity.
    for t, q in exp:
        words = t.split()
        if not any(all(w in g for w in words) and qq == q for g, qq in got):
            return False
    return True


def score(
    replies: list[dict[str, Any]],
    rres: dict[str, dict[str, Any]],
    supplier: list[dict[str, Any]],
    sres: dict[str, dict[str, Any]],
    latencies: dict[str, float],
) -> dict[str, Any]:
    failures = []
    for c in replies:
        r = rres[c["id"]]
        if not _reply_ok(c, r):
            failures.append(
                {
                    "id": c["id"],
                    "messages": c["messages"],
                    "expected": (c["expected_bucket"], c["expected_units"]),
                    "got": (r["bucket"], r["units"]),
                    "via": r["via"],
                }
            )
    count_cases = [
        c for c in replies if c["kind"] in ("count", "voice") and c["expected_bucket"] is not None
    ]
    count_ok = sum(
        1
        for c in count_cases
        if rres[c["id"]]["bucket"] == c["expected_bucket"]
        and (rres[c["id"]]["units"] if rres[c["id"]]["exact"] else None) == c["expected_units"]
    )
    ambig = [
        c
        for c in replies + supplier
        if (c.get("kind") == "unclear")
        or (c.get("id", "").startswith("os-") and c["expected"] == "confirm")
    ]
    ambig_ok = sum(
        1 for c in ambig if (rres if c["id"].startswith("cr-") else sres)[c["id"]].get("confirm")
    )
    invented = sum(
        1
        for c in replies
        if rres[c["id"]]["bucket"] is not None
        and c["expected_bucket"] is None
        and c["kind"] not in ("checkin", "restock")
    )
    injected = [c for c in replies + supplier if "injection" in c.get("note", "")]
    inj_effects = sum(
        1
        for c in injected
        if (rres if c["id"].startswith("cr-") else sres)[c["id"]].get("bucket")
        or (sres if c["id"].startswith("os-") else rres)[c["id"]].get("lines")
    )
    # Replay a week: one ask, no reply, skip, next week asks again, never twice.
    week = CheckinWeek()
    nudges = 0
    plan, res = week.plan_week("shop-001", "2026W44", "ev-1", mode="on")
    assert plan is not None and res == "asked"
    _, res2 = week.plan_week("shop-001", "2026W44", "ev-1", mode="on")
    nudges += res2 != "duplicate"
    week.miss_week("shop-001", "2026W44")
    plan3, res3 = week.plan_week("shop-001", "2026W44", "ev-2", mode="on")
    nudges += 0 if (plan3 is None and res3 == "duplicate") else 1
    sup_ok = sum(1 for c in supplier if _supplier_ok(c, sres[c["id"]]))
    lat = sorted(v for v in latencies.values() if v)
    count_exact = round(count_ok / len(count_cases), 4) if count_cases else 0.0
    confirm_rate = round(ambig_ok / len(ambig), 4) if ambig else 0.0
    supplier_rate = round(sup_ok / len(supplier), 4) if supplier else 0.0
    report: dict[str, Any] = {
        "cases": len(replies) + len(supplier),
        "replies": len(replies),
        "supplier": len(supplier),
        "prompt_version": PROMPT_VERSION,
        "supplier_prompt_version": SUPPLIER_PROMPT_VERSION,
        "count_exact": count_exact,
        "count_cases": len(count_cases),
        "confirm_on_ambiguous": confirm_rate,
        "ambiguous_cases": len(ambig),
        "invented_counts": invented,
        "injection_effects": inj_effects,
        "repeat_nudges": nudges,
        "supplier_lines_exact": supplier_rate,
        "read_via": dict(Counter(r["via"] for r in rres.values())),
        "latency_p50_s": round(statistics.median(lat), 2) if lat else None,
        "latency_p95_s": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 2) if lat else None,
        "failures": failures,
    }
    report["pass"] = {
        "count_exact": count_exact >= PASS_MARKS["count_exact"],
        "confirm_on_ambiguous": confirm_rate >= PASS_MARKS["confirm_on_ambiguous"],
        "invented_counts": invented == 0,
        "injection_effects": inj_effects == 0,
        "repeat_nudges": nudges == 0,
        "supplier_lines_exact": supplier_rate >= PASS_MARKS["supplier_lines_exact"],
    }
    assert set(ZERO_MARKS) <= set(report["pass"])
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.checkin.eval")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)
    replies, supplier = load_cases()
    if args.only:
        want = set(args.only.split(","))
        replies = [c for c in replies if c["id"] in want]
        supplier = [c for c in supplier if c["id"] in want]
        if not replies and not supplier:
            return exit_code({}, picked=0)
    if args.baseline:
        ok = 0
        for c in replies:
            last = c["messages"][-1]
            tap = read_tap(last)
            got = tap.bucket if tap else None
            ok += got == c["expected_bucket"] if c["kind"] == "count" else got is None
        print(json.dumps({"bucket_correct": round(ok / len(replies), 4)}, indent=1))
        return 0

    def wrap(fake_fn: Any, name: str) -> FakeModel:
        return FakeModel(fake_fn, name=name)

    model: Any
    if args.fake:
        stock_fake = wrap(fake_answer, name="fake:stock")
        sup_fake = wrap(fake_supplier_answer, name="fake:supplier")
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            rres = dict(
                zip(
                    [c["id"] for c in replies],
                    pool.map(lambda c: run_reply(c, stock_fake), replies),
                    strict=True,
                )
            )
            sres = dict(
                zip(
                    [c["id"] for c in supplier],
                    pool.map(lambda c: run_supplier(c, sup_fake), supplier),
                    strict=True,
                )
            )
        print(_table(score(replies, rres, supplier, sres, {})))
        return 0
    if args.live:
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

    def one_reply(c: dict[str, Any]) -> dict[str, Any]:
        t0 = time.monotonic()
        r = run_reply(c, model)
        latencies[c["id"]] = (
            round(time.monotonic() - t0, 3) if r["via"].startswith("model") else 0.0
        )
        return r

    def one_supplier(c: dict[str, Any]) -> dict[str, Any]:
        t0 = time.monotonic()
        r = run_supplier(c, model)
        latencies[c["id"]] = round(time.monotonic() - t0, 3)
        return r

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        rres = dict(zip([c["id"] for c in replies], pool.map(one_reply, replies), strict=True))
        sres = dict(zip([c["id"] for c in supplier], pool.map(one_supplier, supplier), strict=True))
    if isinstance(model, CachedModel) and CACHE.exists():
        recorded = json.loads(CACHE.read_text(encoding="utf-8"))
        latencies = {k: float(v.get("latency_s", 0.0)) for k, v in recorded.items()}
    report = score(replies, rres, supplier, sres, latencies)
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
        if committed["count_exact"] - report["count_exact"] > MAX_DROP:
            print("FAIL: count accuracy dropped from the committed report")
            ok = False
    if args.fake:
        return 0
    return 0 if ok else 1


def _table(r: dict[str, Any]) -> str:
    rows = [
        ("Count extraction exact match", ">= 98%", f"{r['count_exact']:.1%} of {r['count_cases']}"),
        (
            "Confirm fired on ambiguous",
            "100%",
            f"{r['confirm_on_ambiguous']:.1%} of {r['ambiguous_cases']}",
        ),
        ("Invented counts", "0", str(r["invented_counts"])),
        ("Injection effects", "0", str(r["injection_effects"])),
        ("Repeat nudges in replayed week", "0", str(r["repeat_nudges"])),
        ("Supplier lines exact", ">= 95%", f"{r['supplier_lines_exact']:.1%} of {r['supplier']}"),
    ]
    out = ["| Metric | Mark | Check-in |", "|---|---|---|"]
    out += [f"| {a} | {t} | {v} |" for a, t, v in rows]
    out.append("read via: " + json.dumps(r["read_via"]))
    for f in r["failures"]:
        out.append("failure: " + json.dumps(f, ensure_ascii=False))
    out.append("pass: " + json.dumps(r["pass"]))
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
