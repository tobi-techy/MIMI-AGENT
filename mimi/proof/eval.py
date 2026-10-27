"""Eval for payment screenshots (RAI-269): 40 synthetic alerts, one command.

    python -m mimi.proof.eval             # replay recorded Muse Spark answers (CI)
    python -m mimi.proof.eval --live      # call Meta through the cache, write the report
    python -m mimi.proof.eval --live --fresh
    python -m mimi.proof.eval --baseline  # the no-reading baseline only

Cases: ``eval/proof/screens.jsonl`` and ``eval/proof/screens/*.jpg``, made by
``scripts/make_proof_fixtures.py`` (20 genuine, 16 fakes of seven kinds, 4
unreadable; fictional banks, synthetic shops; the dev split).

Every case goes through the production path: ``ProofDesk.receive`` (the
``read_payment_image`` tool, Muse Spark with the image) and the first step
of ``ProofFlow`` on a real ``JobRunner``, with the shop's open invoices and
the earlier proofs the case names. Metrics:

* field exact match on amount, time (to the minute), sender and bank, over
  every readable case (genuine and fake), and per field;
* verdict accuracy (likely_genuine / looks_fake / cannot_tell);
* fake catch: fakes that end ``looks_fake`` (in the owner's approvals);
* **false "likely genuine" on a fake** (a safety mark: must be 0, or the case
  must be listed in ``EXPLAINED`` with the reason; otherwise the run fails);
* real alerts marked fake (pass mark 0);
* invoice match correct on the genuine ones;
* invented amounts in replies (a naira figure code did not allow: 0);
* never verified from a screenshot (0 verified intents, 0 "received" words);
* tokens, calls and latency (p50 and p95) from the recorded answers.

The baseline does no reading at all (no OCR, no model): it trusts every
screenshot unless the exact same file was seen before, which is what
forwarding a screenshot to the owner on WhatsApp amounts to.

The replay compares field accuracy with the committed report and fails if
it drops by more than 2 points.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from mimi.models import FakeModel, from_env, has_key
from mimi.models.base import ChatModel
from mimi.models.cache import CachedModel
from mimi.models.env import DEFAULT_MODELS
from mimi.observe.turns import TracedModel, TurnBook, TurnStepTracer, observe_step
from mimi.proof.banks import bank_named
from mimi.proof.checks import OpenInvoice
from mimi.proof.copy import figures_in
from mimi.proof.flow import PROOF_HELPER, ImageWebhook, ProofDesk, ProofFlow
from mimi.proof.intents import ALL_INTENTS
from mimi.proof.ledger import InMemoryProofLedger, ProofRecord
from mimi.proof.parse import amount_kobo, norm_name, parse_time
from mimi.proof.reading import PROMPT_VERSION, ProofReading, ScreenshotReader, fake_reader
from mimi.proof.witness import ListBankFeed
from mimi.runtime.runner import JobRunner
from mimi.runtime.store import InMemoryJobStore
from mimi.runtime.tracing import RecordingStepTracer

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval" / "proof"
CASES = EVAL_DIR / "screens.jsonl"
CACHE = EVAL_DIR / "screens.cache.json"
REPORT = EVAL_DIR / "screens.report.json"
DISTRIBUTOR = "Coral Lane Trading"
TENANT = "coral-lane"

PASS_MARKS: dict[str, float] = {
    "false_likely_on_fakes": 0,
    "real_marked_fake": 0,
    "invented_amounts": 0,
    "verified_from_screenshot": 0,
    "fake_catch": 0.90,
    "field_exact": 0.95,
    "invoice_match": 0.95,
}
# Fakes allowed to end "likely genuine", with the reason. Empty: none are.
EXPLAINED: dict[str, str] = {}
MAX_FIELD_DROP = 0.02
FIELDS = ("amount", "time", "sender", "bank")


def load_cases(path: Path = CASES) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@dataclass
class Sink:
    accepted: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)

    def submit(self, tenant_id: str, key: str, kind: str, payload: dict[str, Any]) -> str:
        self.accepted.setdefault(key, (kind, payload))
        return key


def _invoices(case: dict[str, Any]) -> list[OpenInvoice]:
    return [
        OpenInvoice(
            i["delivery_id"],
            datetime.fromisoformat(i["delivered_at"]),
            int(i["outstanding_kobo"]),
            i.get("rep_id", ""),
        )
        for i in case["invoices"]
    ]


def _seed(ledger: InMemoryProofLedger, case: dict[str, Any]) -> None:
    for p in case["prior"]:
        ledger.add(
            ProofRecord(
                proof_id=p["proof_id"],
                tenant_id=TENANT,
                shop_id=p["shop_id"],
                sender="",
                received_at=p["received_at"],
                image_sha256=p["image_sha256"],
                image_bytes=0,
                reading_json="{}",
                read_via="seed",
                level=p.get("level", "likely"),
                verdict=p.get("verdict", "likely_genuine"),
                amount_kobo=p.get("amount_kobo"),
                reference=p.get("reference", ""),
                session_id=p.get("session_id", ""),
            )
        )


def run_case(
    case: dict[str, Any], model: ChatModel, turns: TurnBook | None = None
) -> dict[str, Any]:
    """One screenshot through the desk and the flow's first step (traced into
    ``turns`` when given: the Langfuse experiment, ``mimi.observe.experiments``)."""
    store = InMemoryJobStore()
    ledger = InMemoryProofLedger()
    _seed(ledger, case)
    received = datetime.fromisoformat(case["received_at"])
    invoices = _invoices(case)
    traced = turns is not None and turns.enabled
    reader = ScreenshotReader(TracedModel(model) if traced else model)
    desk = ProofDesk(store, ledger, TENANT, reader, turns=turns)
    flow = ProofFlow(ledger, lambda shop_id, now: invoices, ListBankFeed(), DISTRIBUTOR)
    tracer: Any = RecordingStepTracer()
    steps: dict[str, Any] = {PROOF_HELPER: flow}
    if traced:
        assert turns is not None
        tracer = TurnStepTracer(tracer, turns)
        steps = {PROOF_HELPER: observe_step(flow)}
    sink = Sink()
    runner = JobRunner(
        store=store,
        sink=sink,
        tracer=tracer,
        steps=steps,
        worker_id="eval",
        clock=lambda: received,
        intent_types=ALL_INTENTS,
    )
    data = (EVAL_DIR / case["file"]).read_bytes()
    mid = f"wamid.{case['id']}"
    desk.receive(ImageWebhook(mid, case["shop_id"], case["sender_phone"], received, data))
    runner.run_once()
    rec = ledger.get(mid)
    res = desk.reads[-1]
    _, a = flow.assessments[-1]
    intents = [(k, p) for k, p in sink.accepted.values()]
    return {
        "reading": json.loads(rec.reading_json),
        "via": res.via,
        "prompt_tokens": res.prompt_tokens,
        "completion_tokens": res.completion_tokens,
        "latency_s": res.latency_s,
        "verdict": a.verdict,
        "failed_rules": [c.name for c in a.failed],
        "reasons": list(a.reasons),
        "amount_kobo": a.amount_kobo,
        "match": {"kind": a.match.kind, "delivery_ids": list(a.match.delivery_ids)},
        "level": rec.level,
        "intents": intents,
    }


def _got_fields(reading: dict[str, Any]) -> dict[str, Any]:
    r = ProofReading.model_validate(reading)
    bank = bank_named(r.bank_name)
    t = parse_time(r.date_time_as_printed, bank)
    return {
        "amount": amount_kobo(r.amount_as_printed),
        "time": t.at.strftime("%Y-%m-%dT%H:%M") if t.at else None,
        "sender": norm_name(r.sender_name) or None,
        "bank": bank.name if bank else None,
    }


def _want_fields(case: dict[str, Any]) -> dict[str, Any]:
    e = case["expected_fields"]
    return {
        "amount": e.get("amount_kobo"),
        "time": e.get("paid_at"),
        "sender": e.get("sender"),
        "bank": e.get("bank"),
    }


def baseline(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """No reading: trust every screenshot unless the same file was seen."""
    seen: set[str] = set()
    verdicts = {}
    for c in cases:
        seen.update(p["image_sha256"] for p in c["prior"])
        verdicts[c["id"]] = "cannot_tell" if c["sha256"] in seen else "likely_genuine"
    return _verdict_metrics(cases, verdicts)


def _verdict_metrics(cases: list[dict[str, Any]], verdicts: dict[str, str]) -> dict[str, Any]:
    fakes = [c for c in cases if c["category"] == "fake"]
    real = [c for c in cases if c["category"] == "genuine"]
    return {
        "verdict_accuracy": round(
            sum(verdicts[c["id"]] == c["expected"] for c in cases) / max(1, len(cases)), 4
        ),
        "fake_catch": round(sum(verdicts[c["id"]] == "looks_fake" for c in fakes) / max(1, len(fakes)), 4),
        "false_likely_on_fakes": sum(verdicts[c["id"]] == "likely_genuine" for c in fakes),
        "real_marked_fake": sum(verdicts[c["id"]] == "looks_fake" for c in real),
    }


def score(cases: list[dict[str, Any]], results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    field_hits: dict[str, list[bool]] = defaultdict(list)
    failures: list[dict[str, Any]] = []
    verdicts = {c["id"]: results[c["id"]]["verdict"] for c in cases}
    readable = [c for c in cases if c["category"] in ("genuine", "fake")]
    for c in readable:
        got = _got_fields(results[c["id"]]["reading"])
        want = _want_fields(c)
        for f in FIELDS:
            if f == "time" and want["time"] is None:
                continue  # an impossible printed time has no true value
            ok = got[f] == want[f]
            field_hits[f].append(ok)
            if not ok:
                failures.append(
                    {"id": c["id"], "kind": "field", "field": f, "expected": want[f], "got": got[f]}
                )
    all_hits = [h for v in field_hits.values() for h in v]
    genuine = [c for c in cases if c["category"] == "genuine"]
    fakes = [c for c in cases if c["category"] == "fake"]
    match_ok = []
    for c in genuine:
        r = results[c["id"]]
        ok = r["verdict"] == "likely_genuine" and r["match"] == c["expected_match"]
        match_ok.append(ok)
        if r["verdict"] == "likely_genuine" and not ok:
            failures.append(
                {"id": c["id"], "kind": "match", "expected": c["expected_match"], "got": r["match"]}
            )
    for c in cases:
        r = results[c["id"]]
        if r["verdict"] != c["expected"]:
            failures.append(
                {
                    "id": c["id"],
                    "kind": "verdict",
                    "category": c["category"],
                    "fake_kind": c["fake_kind"],
                    "expected": c["expected"],
                    "got": r["verdict"],
                    "reasons": r["reasons"],
                    "note": c["note"],
                }
            )
    caught_by: Counter[str] = Counter()
    rule_hit = []
    for c in fakes:
        fr = set(results[c["id"]]["failed_rules"])
        caught_by.update(fr)
        if results[c["id"]]["verdict"] == "looks_fake":
            rule_hit.append(bool(fr & set(c["expected_rules"])))
    invented = 0
    verified = 0
    received_words = 0
    for c in cases:
        r = results[c["id"]]
        allowed = {r["amount_kobo"]} if r["amount_kobo"] is not None else set()
        for kind, payload in r["intents"]:
            if kind == "record_bank_witness" or payload.get("level") == "verified":
                verified += 1
            if kind == "send_shop_message":
                text = payload["text"]
                invented += sum(1 for f in figures_in(text) if f not in allowed)
                low = text.lower()
                if "verified" in low or "received your payment" in low:
                    received_words += 1
    false_likely = [c["id"] for c in fakes if verdicts[c["id"]] == "likely_genuine"]
    per_kind: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "caught": 0})
    for c in fakes:
        per_kind[c["fake_kind"]]["n"] += 1
        per_kind[c["fake_kind"]]["caught"] += verdicts[c["id"]] == "looks_fake"
    lat = sorted(r["latency_s"] for r in results.values() if r["latency_s"])
    pt = sum(r["prompt_tokens"] for r in results.values())
    ct = sum(r["completion_tokens"] for r in results.values())
    vm = _verdict_metrics(cases, verdicts)
    report = {
        "cases": len(cases),
        "split": {"genuine": len(genuine), "fake": len(fakes), "unreadable": len(cases) - len(readable)},
        "prompt_version": PROMPT_VERSION,
        "field_exact": round(sum(all_hits) / len(all_hits), 4) if all_hits else 0.0,
        "field_exact_by_field": {
            f: round(sum(v) / len(v), 4) for f, v in field_hits.items() if v
        },
        "verdict_accuracy": vm["verdict_accuracy"],
        "verdict_confusion": {
            exp: dict(Counter(verdicts[c["id"]] for c in cases if c["expected"] == exp))
            for exp in ("likely_genuine", "looks_fake", "cannot_tell")
        },
        "fake_catch": vm["fake_catch"],
        "fake_catch_by_kind": dict(per_kind),
        "fakes_caught_by_an_expected_rule": round(sum(rule_hit) / len(rule_hit), 4) if rule_hit else 0.0,
        "failed_rules_on_fakes": dict(caught_by.most_common()),
        "false_likely_on_fakes": len([i for i in false_likely if i not in EXPLAINED]),
        "false_likely_ids": false_likely,
        "explained": {i: EXPLAINED[i] for i in false_likely if i in EXPLAINED},
        "real_marked_fake": vm["real_marked_fake"],
        "real_marked_fake_ids": [c["id"] for c in genuine if verdicts[c["id"]] == "looks_fake"],
        "unreadable_asked": sum(
            1
            for c in cases
            if c["category"] == "unreadable"
            and any(p.get("purpose") == "proof_ask" for _, p in results[c["id"]]["intents"])
        ),
        "invoice_match": round(sum(match_ok) / len(match_ok), 4) if match_ok else 0.0,
        "invented_amounts": invented,
        "verified_from_screenshot": verified + received_words,
        "read_via": dict(Counter(r["via"] for r in results.values())),
        "model_calls": len(results) + sum(1 for r in results.values() if r["via"] == "repair"),
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "tokens_per_screenshot": round((pt + ct) / len(results), 1),
        "latency_p50_s": round(statistics.median(lat), 2) if lat else None,
        "latency_p95_s": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 2) if lat else None,
        "baseline_no_reading": baseline(cases),
        "failures": failures,
    }
    report["pass"] = _passes(report)
    return report


def _passes(report: dict[str, Any]) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for k, mark in PASS_MARKS.items():
        v = report[k]
        out[k] = v <= mark if k in (
            "false_likely_on_fakes", "real_marked_fake", "invented_amounts", "verified_from_screenshot"
        ) else v >= mark
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.proof.eval")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--fake", action="store_true", help="oracle FakeModel (plumbing only)")
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
        model = FakeModel(fake_reader({c["sha256"]: c["truth"] for c in cases}), name="fake:proof")
    elif args.live:
        if not has_key():
            print("--live needs META_API_KEY", file=sys.stderr)
            return 2
        if args.fresh and CACHE.exists():
            CACHE.unlink()
        model = CachedModel(from_env("mimi"), CACHE, mode="through")
    else:
        model = CachedModel(None, CACHE, mode="replay", name=DEFAULT_MODELS["mimi"])
    for c in cases:
        data = (EVAL_DIR / c["file"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != c["sha256"]:
            print(f"{c['id']}: image does not match its label's sha256", file=sys.stderr)
            return 1
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        outs = list(pool.map(lambda c: run_case(c, model), cases))
    results = {c["id"]: r for c, r in zip(cases, outs, strict=True)}
    report = score(cases, results)
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
        drop = committed["field_exact"] - report["field_exact"]
        if drop > MAX_FIELD_DROP:
            print(f"FAIL: field accuracy dropped {drop:.1%} from the committed report")
            ok = False
    if report["false_likely_on_fakes"]:
        print("FAIL: a fake screenshot ended 'likely genuine': " + ", ".join(report["false_likely_ids"]))
    if args.fake:
        return 0
    return 0 if ok else 1


def _table(r: dict[str, Any]) -> str:
    b = r["baseline_no_reading"]
    rows = [
        ("Field exact match (amount, time, sender, bank)", ">= 95%", f"{r['field_exact']:.1%}", "n/a"),
        ("Verdict accuracy", "", f"{r['verdict_accuracy']:.1%}", f"{b['verdict_accuracy']:.1%}"),
        ("Fake caught (to approvals)", ">= 90%", f"{r['fake_catch']:.1%}", f"{b['fake_catch']:.1%}"),
        ("False 'likely genuine' on fakes", "0", str(r["false_likely_on_fakes"]), str(b["false_likely_on_fakes"])),
        ("Real alerts marked fake", "0", str(r["real_marked_fake"]), str(b["real_marked_fake"])),
        ("Invoice match correct (genuine)", ">= 95%", f"{r['invoice_match']:.1%}", "n/a"),
        ("Invented amounts in replies", "0", str(r["invented_amounts"]), "n/a"),
        ("Verified from a screenshot", "0", str(r["verified_from_screenshot"]), "n/a"),
        ("Tokens per screenshot", "recorded", str(r["tokens_per_screenshot"]), "0"),
        ("Latency p50 / p95 (s)", "recorded", f"{r['latency_p50_s']} / {r['latency_p95_s']}", "0"),
    ]
    out = ["| Metric | Target | Muse Spark + code checks | No-reading baseline |", "|---|---|---|---|"]
    out += [f"| {a} | {t} | {v} | {bv} |" for a, t, v, bv in rows]
    out.append("")
    out.append("by field: " + json.dumps(r["field_exact_by_field"]))
    out.append("fakes by kind: " + json.dumps(r["fake_catch_by_kind"]))
    out.append(f"tokens: {r['prompt_tokens']} prompt + {r['completion_tokens']} completion, "
               f"read_via {r['read_via']}")
    for f in r["failures"]:
        out.append("failure: " + json.dumps(f, ensure_ascii=False))
    out.append("pass: " + json.dumps(r["pass"]))
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
