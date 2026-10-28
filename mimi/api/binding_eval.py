"""Held-out eval for pending-ask binding (RAI-272).

    python -m mimi.api.binding_eval          # replay the recorded answers (CI)
    python -m mimi.api.binding_eval --live   # record what is missing (META_API_KEY)

Cases: ``eval/binding/receipt_answers.heldout.jsonl``, 43 replies a shop sends
while its delivery receipt is open (English, Pidgin and Yoruba-flavoured
English, short forms such as "2 cartons short", "e no complete", "ko pe",
"YES", "ok received", damaged and wrong goods, and unrelated questions). The
set was written and committed before the binder; it is held out: nothing was
tuned on it.

Each case goes through the API's own path, with Muse Spark behind every
model call (recorded once, replayed in CI):

1. The router, as the agent builds it (rules, phrases, the fast classifier,
   the binder, the routing model), with the thread waiting on the receipt
   (``order/drop-confirmation``, sent 20 minutes before, about that
   delivery).
2. Bound to Order: Order's reply reader (``ReceiptJudge``) and the code
   guards (``decide``) give the action: consent, flag_dispute, clarify or
   flag_unclear. Not bound: the action is ``none`` (the normal router has
   the message, and the receipt stays open).

Scored in code. A case is correct when the route is right (bound, released,
or either for an injection) and the action is one of the case's
``ok_actions``. Pass marks: 95% correct, 0 false consents (consent on a case
whose ok actions do not include consent).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from mimi.harness import fastclass
from mimi.harness.binding import BINDER_PROMPT_VERSION, AnswerBinder
from mimi.harness.gateway import kobo_cost
from mimi.harness.inbound import Directory, Inbound, Payload
from mimi.harness.router import Router, ThreadState
from mimi.harness.versions import ROUTER_VERSIONS
from mimi.models.base import ChatModel
from mimi.models.cache import CachedModel
from mimi.models.env import DEFAULT_MODELS, from_env
from mimi.receipts.decide import decide
from mimi.receipts.inbox import ShopReply
from mimi.receipts.receipt import receipt_text
from mimi.receipts.verdict import ReceiptJudge
from mimi.world.loader import load_world

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval" / "binding"
CASES = EVAL_DIR / "receipt_answers.heldout.jsonl"
ROUTER_CACHE = EVAL_DIR / "router.cache.json"
JUDGE_CACHE = EVAL_DIR / "judge.cache.json"
REPORT = EVAL_DIR / "binding.report.json"
SKILL = "order/drop-confirmation"
PASS_MARKS = {"correct": 0.95, "false_consents": 0}


def load_cases(path: Path = CASES) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _models(live: bool) -> tuple[CachedModel, CachedModel]:
    name = os.environ.get("MIMI_MODEL", "").strip() or DEFAULT_MODELS["mimi"]
    if live:
        if not os.environ.get("META_API_KEY", "").strip():
            raise SystemExit("--live needs META_API_KEY")
        inner: ChatModel | None = from_env("mimi")
        mode = "through"
    else:
        inner, mode = None, "replay"
    return (CachedModel(inner, ROUTER_CACHE, mode=mode, name=name,  # type: ignore[arg-type]
                        versions=ROUTER_VERSIONS),
            CachedModel(inner, JUDGE_CACHE, mode=mode, name=name))  # type: ignore[arg-type]


def run(live: bool = False, cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    from mimi.store import SqlGo, ensure_schema, make_engine, seed_world

    world = load_world()
    engine = make_engine("sqlite://")
    ensure_schema(engine)
    seed_world(engine, world)
    directory: Directory = SqlGo(engine).directory(world.tenant_id)
    router_model, judge_model = _models(live)
    router = Router(model=router_model, fast=fastclass.model(), price=kobo_cost,
                    binder=AnswerBinder(model=router_model))
    judge = ReceiptJudge(judge_model)
    from mimi.api.e2e.worker import SIM_NOW

    asked = SIM_NOW - timedelta(minutes=20)
    rows: list[dict[str, Any]] = []
    cost = 0
    for c in cases if cases is not None else load_cases():
        delivery = world.delivery(c["delivery_id"])
        shop = world.shop(c["shop_id"])
        thread = ThreadState(pending_skill=SKILL, expects="yes_no", asked_at=asked.isoformat(),
                             ask_ref=delivery.delivery_id, last_skill=SKILL)
        inbound = Inbound(source="shop_message", org_id=world.tenant_id,
                          external_id=f"heldout.{c['id']}", received_at=SIM_NOW,
                          payload=Payload(text=c["text"], from_phone=shop.phone))
        dec = router.route(inbound, directory, thread)
        cost += dec.cost_kobo
        bound = dec.status == "routed" and dec.skill == SKILL
        action, outcome = "none", ""
        if bound:
            reply = ShopReply(f"wamid.{c['id']}", f"turn.{c['id']}", shop.shop_id, "shop",
                              c["text"], SIM_NOW.isoformat())
            j = judge(delivery=delivery, earlier=[], new=[reply],
                      mimi_sent=[receipt_text(delivery, shop.name, world.distributor)])
            cost += kobo_cost(judge_model.name, j.prompt_tokens, j.completion_tokens)
            d = decide(j.verdict, delivery=delivery, replies=[reply], clarified=False)
            action, outcome = d.action, j.verdict.outcome
        want = c["expect_route"]
        route_ok = (want == "either" or (want == "bind") == bound)
        ok = route_ok and action in c["ok_actions"]
        rows.append({"id": c["id"], "label": c["label"], "text": c["text"],
                     "route": dec.skill if dec.status == "routed" else "clarify",
                     "rule": dec.rule, "bound": dec.bound, "route_ok": route_ok,
                     "action": action, "outcome": outcome, "ok": ok,
                     "false_consent": action == "consent" and not c["consent_ok"]})
    return score(rows, cost_kobo=cost, live=live)


def score(rows: list[dict[str, Any]], *, cost_kobo: int = 0, live: bool = False
          ) -> dict[str, Any]:
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["label"]].append(r)
    n = len(rows)
    correct = sum(r["ok"] for r in rows)
    fc = [r["id"] for r in rows if r["false_consent"]]
    rate = round(correct / n, 4) if n else 0.0
    return {
        "cases": n, "correct": correct, "correct_rate": rate,
        "routing_rate": round(sum(r["route_ok"] for r in rows) / n, 4) if n else 0.0,
        "false_consents": fc,
        "by_label": {k: f"{sum(r['ok'] for r in v)}/{len(v)}" for k, v in sorted(by.items())},
        "misses": [r for r in rows if not r["ok"]],
        "rows": rows, "cost_kobo": cost_kobo, "mode": "live" if live else "replay",
        "binder_prompt_version": BINDER_PROMPT_VERSION, "pass_marks": PASS_MARKS,
        "passed": rate >= PASS_MARKS["correct"] and len(fc) <= PASS_MARKS["false_consents"],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.api.binding_eval")
    ap.add_argument("--live", action="store_true", help="record missing answers")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)
    rep = run(live=args.live)
    print(f"pending-ask binding, held out: {rep['correct']}/{rep['cases']} correct "
          f"({rep['correct_rate']:.1%}), routing {rep['routing_rate']:.1%}, "
          f"false consents {len(rep['false_consents'])}, cost N{rep['cost_kobo'] / 100:.2f}")
    print("by label: " + ", ".join(f"{k} {v}" for k, v in rep["by_label"].items()))
    for m in rep["misses"]:
        print(f"  miss {m['id']} [{m['label']}] {m['text']!r}: route {m['route']} "
              f"({m['rule']}), action {m['action']} ({m['outcome']})")
    if not args.no_write:
        REPORT.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    print("PASSED" if rep["passed"] else "FAILED")
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
