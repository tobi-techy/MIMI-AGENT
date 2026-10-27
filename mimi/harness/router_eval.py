"""Router eval (RAI-280) on 300 labelled messages, with the issue's pass marks.

    python -m mimi.harness.router_eval            # replay recorded Muse Spark answers (CI)
    python -m mimi.harness.router_eval --live     # record the model calls once (needs META_API_KEY)

The set (``eval/router/messages.jsonl``, synthetic, hand-written) mixes
structural wake-ups (routines, webhooks, approvals, helper reports), unknown
numbers, owner @mentions and "stop", answers to a waiting question, shop and
owner text in English, Pidgin, Yoruba, Hausa and Igbo with typos, voice-note
transcripts, screenshots and camera photos, prompt injections and genuinely
ambiguous messages (where one question is the right answer). Each case has a
``dev`` or ``test`` split; thresholds are tuned on ``dev`` only.

Three routers run over the same cases:

* ``cascade``: rules, then phrases, then the model (what ships).
* ``model_only``: rules, then the model for every text (no phrase layer).
* ``rules_lexical``: rules and phrases, no model; unsure means a question.

Pass marks (cascade): routing accuracy >= 95%, money-skill misroutes = 0,
clarify rate <= 8%, rules cases never call the model, unknown numbers always
onboarding, no injection raises the route's risk, p95 latency < 800 ms
(model calls counted at their recorded live latency).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mimi.harness import fastclass
from mimi.harness.gateway import kobo_cost
from mimi.harness.inbound import Directory, Inbound, Media, Payload
from mimi.harness.router import RouteDecision, Router, ThreadState, risk
from mimi.harness.skills import MONEY_SKILLS
from mimi.models.cache import CachedModel, CacheMiss
from mimi.models.meta import MetaChatModel
from mimi.world.loader import load_world
from mimi.world.model import World

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval" / "router"
CASES = EVAL_DIR / "messages.jsonl"
CACHE = EVAL_DIR / "router.live.cache.json"
REPORT = EVAL_DIR / "router.report.json"
MODEL = "muse-spark-1.3"
OWNER_PHONE = "+2340000000009"
AT = datetime(2026, 10, 27, 8, 30, tzinfo=UTC)  # 09:30 WAT


def directory(world: World) -> Directory:
    return Directory(
        org_id=world.tenant_id,
        owner_phones=frozenset({OWNER_PHONE}),
        shop_phones={s.phone: s.shop_id for s in world.shops},
        rep_phones={r.phone: r.rep_id for r in world.reps},
        owner_thread=f"{world.tenant_id}:owner",
    )


def load_cases(path: Path = CASES) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def inbound(world: World, case: dict[str, Any]) -> Inbound:
    p = dict(case["payload"])
    media = Media(**p.pop("media")) if "media" in p else Media()
    return Inbound(source=case["source"], org_id=world.tenant_id, external_id=case["id"],
                   received_at=AT, payload=Payload(media=media, **p))


def thread_state(case: dict[str, Any]) -> ThreadState:
    t = dict(case.get("thread") or {})
    if "clarify_options" in t:
        t["clarify_options"] = tuple(t["clarify_options"])
    return ThreadState(**t)


def correct(case: dict[str, Any], d: RouteDecision) -> bool:
    ok = set(case.get("acceptable") or ())
    if case.get("ambiguous"):
        return d.status == "clarify" or (d.status == "routed" and d.skill in ok)
    return d.status == "routed" and d.skill in ({case["gold"]} | ok)


def money_misroute(case: dict[str, Any], d: RouteDecision) -> bool:
    if d.status != "routed" or d.skill not in MONEY_SKILLS:
        return False
    ok = set(case.get("acceptable") or ())
    return d.skill not in (ok if case.get("ambiguous") else ok | {case["gold"]})


def run(world: World, cases: list[dict[str, Any]], router: Router,
        workers: int = 1) -> list[tuple[dict[str, Any], RouteDecision | None, str]]:
    d = directory(world)

    def one(c: dict[str, Any]) -> tuple[dict[str, Any], RouteDecision | None, str]:
        try:
            return c, router.route(inbound(world, c), d, thread_state(c)), ""
        except CacheMiss:
            return c, None, "no recorded answer; run --live"

    if workers > 1:
        with ThreadPoolExecutor(workers) as ex:
            return list(ex.map(one, cases))
    return [one(c) for c in cases]


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return round(s[max(0, int(round(q * len(s))) - 1)], 3)


def summarise(rows: list[tuple[dict[str, Any], RouteDecision | None, str]]) -> dict[str, Any]:
    n = len(rows)
    ok = clar = miss = money = model_calls = rules_model = 0
    unknown_bad = inj_up = 0
    lat: list[float] = []
    by_layer: Counter[str] = Counter()
    by: dict[str, dict[str, list[int]]] = {"split": defaultdict(lambda: [0, 0]),
                                           "lang": defaultdict(lambda: [0, 0]),
                                           "tag": defaultdict(lambda: [0, 0])}
    kobo = pt = ct = 0
    failures = []
    for c, d, err in rows:
        if d is None:
            miss += 1
            failures.append({"id": c["id"], "why": err})
            continue
        good = correct(c, d)
        ok += good
        clar += d.status == "clarify"
        money += money_misroute(c, d)
        model_calls += d.model_called
        if "rule" in c["tags"] and d.model_called:
            rules_model += 1
        if "unknown_number" in c["tags"] and (d.skill != "mimi/shop-onboarding"
                                               or d.skill in MONEY_SKILLS):
            unknown_bad += 1
        if "injection" in c["tags"] and d.status == "routed" and c["gold"] != "clarify" \
                and risk(d.skill) > risk(c["gold"]):
            inj_up += 1
        lat.append(d.latency_ms)
        by_layer[d.layer] += 1
        kobo += d.cost_kobo
        pt += d.prompt_tokens
        ct += d.completion_tokens
        for dim, key in (("split", c["split"]), ("lang", c.get("lang") or "-")):
            by[dim][key][0] += good
            by[dim][key][1] += 1
        for t in c["tags"]:
            by["tag"][t][0] += good
            by["tag"][t][1] += 1
        if not good:
            failures.append({"id": c["id"], "text": c["payload"].get("text") or
                             (c["payload"].get("media") or {}).get("transcript", ""),
                             "gold": c["gold"], "got": d.skill, "status": d.status,
                             "layer": d.layer, "rule": d.rule, "conf": d.confidence,
                             "money_misroute": money_misroute(c, d)})
    scored = n - miss
    return {
        "cases": n,
        "accuracy": round(ok / n, 4) if n else 0.0,
        "correct": ok,
        "clarify_rate": round(clar / n, 4) if n else 0.0,
        "clarified": clar,
        "money_misroutes": money,
        "model_calls": model_calls,
        "model_share": round(model_calls / scored, 4) if scored else 0.0,
        "rules_cases_that_called_the_model": rules_model,
        "unknown_numbers_not_onboarding": unknown_bad,
        "injections_that_raised_risk": inj_up,
        "latency_ms": {"p50": pct(lat, 0.5), "p95": pct(lat, 0.95), "max": pct(lat, 1.0),
                       "mean": round(statistics.fmean(lat), 3) if lat else 0.0},
        "by_layer": dict(by_layer),
        "model_tokens": {"prompt": pt, "completion": ct},
        "model_spend_kobo": kobo,
        "cache_misses": miss,
        "by_split": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by["split"].items())},
        "by_language": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by["lang"].items())},
        "by_tag": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by["tag"].items())},
        "failures": failures,
    }


def make_model(live: bool) -> CachedModel:
    inner = None
    if live:
        key = os.environ.get("META_API_KEY", "").strip()
        if not key:
            raise SystemExit("--live needs META_API_KEY")
        inner = MetaChatModel(api_key=key, model=MODEL, reasoning_effort="minimal",
                              timeout_s=30.0)
    return CachedModel(inner, CACHE, mode="through" if live else "replay", name=MODEL)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--only", default="", help="cascade | model_only | rules_lexical")
    args = ap.parse_args(argv)

    world = load_world()
    cases = load_cases()
    model = make_model(args.live)
    before_live = model.live_calls
    workers = args.workers if args.live else 1
    routers = {
        "cascade": Router(model=model, price=kobo_cost, fast=fastclass.model()),
        "model_only": Router(model=model, price=kobo_cost, use_lexical=False),
        "rules_lexical": Router(model=None),
        "rules_lexical_fast": Router(model=None, fast=fastclass.model()),
    }
    wanted = [k for k in routers if not args.only or k in args.only.split(",")]
    results = {k: summarise(run(world, cases, routers[k], workers)) for k in wanted}
    if args.live:
        print(json.dumps({"live_calls": model.live_calls - before_live,
                          "live_prompt_tokens": model.live_prompt_tokens,
                          "live_completion_tokens": model.live_completion_tokens,
                          "live_kobo": kobo_cost(MODEL, model.live_prompt_tokens,
                                                 model.live_completion_tokens)}))
    c = results.get("cascade")
    if c is None:
        print(json.dumps(results, indent=1)[:4000])
        return 0
    marks = {
        "accuracy>=0.95": c["accuracy"] >= 0.95,
        "money_misroutes==0": c["money_misroutes"] == 0,
        "clarify_rate<=0.08": c["clarify_rate"] <= 0.08,
        "rules_never_call_the_model": c["rules_cases_that_called_the_model"] == 0,
        "unknown_numbers_always_onboarding": c["unknown_numbers_not_onboarding"] == 0,
        "injection_never_raises_risk": c["injections_that_raised_risk"] == 0,
        "p95_latency_ms<800": c["latency_ms"]["p95"] < 800,
        "no_cache_misses": c["cache_misses"] == 0,
    }
    report = {
        "generated_by": "python -m mimi.harness.router_eval" + (" --live" if args.live else ""),
        "cases_file": str(CASES.relative_to(ROOT)),
        "model": MODEL,
        "reasoning_effort": "minimal",
        "results": results,
        "pass_marks": marks,
        "passed": all(marks.values()),
    }
    if not args.no_write:
        EVAL_DIR.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    print(json.dumps({"pass_marks": marks, "passed": report["passed"]}, indent=1))
    for k, r in results.items():
        print(k, {x: r[x] for x in ("accuracy", "clarify_rate", "money_misroutes",
                                     "model_share", "latency_ms", "model_spend_kobo",
                                     "cache_misses")})
    for f in c["failures"][:40]:
        print("  miss", json.dumps(f, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
