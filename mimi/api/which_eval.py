"""Held-out eval: a delivery receipt and a correction question open on one thread (RAI-272).

    python -m mimi.api.which_eval [--no-write]

Cases: ``eval/which/cases.jsonl``, 40 shop replies, written and committed
before the fix (7f82faa) and never tuned on. Each case opens a delivery
receipt ask, a correction question (about a pending restock draft or a
credit request), or both, on shop-012's thread, then sends the shop's reply
(or two) through the API agent's own path: the durable store, the router,
the steer stage and the helpers. No model is called: the outcome of every
reply here is decided by code (the binder and the steer classifier fall
back without a model, and the helpers run on the offline model), so the
eval runs offline and needs no recording.

Each reply's outcome, read from the store:

* ``which``: Mimi asked one code-written question ("Yes to the delivery
  receipt or the order change?"), nothing was bound, both asks stay open;
* ``receipt``: Order has the reply (the delivery receipt), the correction stays open;
* ``fix``: the reply answered the correction question, the receipt stays open;
* ``owner``: still unclear after the question; the owner got a note, no second question.

Pass marks: 95% of cases correct, and 0 binds on an ambiguous reply (a
reply whose expected outcome is ``which`` or ``owner`` that went to Order
or to the correction instead). Every case also checks that no consent was
recorded and no approval changed, except where the case says so.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from mimi.api import steering as sx
from mimi.harness import steer as st
from mimi.harness.inbound import Inbound, Payload
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.store.util import dumps, insert_ignore, loads, now_iso
from mimi.world.loader import load_world

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval" / "which"
CASES = EVAL_DIR / "cases.jsonl"
REPORT = EVAL_DIR / "which.report.json"
PASS_MARKS = {"correct": 0.95, "ambiguous_binds": 0}
TENANT = "coral-lane"
SHOP = "shop-012"
THREAD = f"{TENANT}:shop:{SHOP}"
T0 = datetime(2026, 10, 26, 21, 0, tzinfo=UTC)
WORDS = {"credit": "abeg make am 300k not 200k", "restock": "make am 6 cartons"}


def load_cases(path: Path = CASES) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _setup(engine: Any, world: Any, case: dict[str, Any]) -> None:
    """The thread as the helpers leave it: a receipt sent, a correction asked."""
    d = next(x for x in world.live_deliveries if x.shop_id == SHOP)
    state: dict[str, Any] = {}
    with engine.begin() as conn:
        if case["task"]:
            tool = "submit_credit_review" if case["task"] == "credit" else "draft_restock"
            helper, skill = (("credit", "credit/credit-request") if case["task"] == "credit"
                             else ("stock", "stock/stock-count"))
            key = f"key_which_{case['id']}"
            payload = ({"shop_id": SHOP, "requested_kobo": 20_000_000} if tool ==
                       "submit_credit_review" else {"sku": "MLK-TIN-CTN", "quantity": 4,
                                                    "unit": "carton"})
            insert_ignore(conn, s.approvals, {
                "approval_id": f"apr_{case['id']}", "tenant_id": TENANT,
                "idempotency_key": key, "turn_id": "t", "thread_id": THREAD,
                "helper": helper, "skill": skill, "shop_id": SHOP, "tool": tool,
                "intent_kind": tool, "preview": "", "payload": dumps(payload),
                "status": "pending", "decided_by": "", "note": "", "created_at": now_iso(),
                "created_ts": time.time(), "decided_at": None})
            insert_ignore(conn, s.intents, {
                "tenant_id": TENANT, "idempotency_key": key, "attempt_key": "",
                "turn_id": "t", "thread_id": THREAD, "tool": tool, "intent_kind": tool,
                "tier": "approve", "shop_id": SHOP, "payload": dumps(payload), "preview": "",
                "outcome": "queued_for_approval", "created_at": now_iso()})
    if case["task"] and case["open"] in ("both", "fix"):
        tasks = sx.open_tasks(engine, TENANT, THREAD, SHOP)
        task = tasks[0]
        words = WORDS[case["task"]]
        state["fix"] = json.dumps({
            "task": sx.task_json(task), "label": "corrects", "words": words,
            "question": st.question_text("corrects", words, tasks, task, words),
            "asked_ts": time.time(), "tasks": [t.key for t in tasks]}, sort_keys=True)
    if case["open"] in ("both", "receipt"):
        state.update(pending_skill="order/drop-confirmation", expects="yes_no",
                     asked_at=(T0 - timedelta(minutes=20)).isoformat(), ask_ref=d.delivery_id)
    with engine.begin() as conn:
        insert_ignore(conn, s.threads, {
            "tenant_id": TENANT, "thread_id": THREAD, "kind": "shop", "shop_id": SHOP,
            "summary": "", "claim_seq": 0, "last_at": now_iso(), "last_ts": time.time(),
            "created_at": now_iso(), "state": dumps(state)})


def outcome_of(turn: Any) -> str:
    res = loads(turn.result, {})
    which = (res.get("steer") or {}).get("which") or {}
    if turn.status == "steer_which":
        return "which"
    if turn.status == "steer_which_owner":
        return "owner"
    if turn.skill == "order/drop-confirmation":
        return "receipt"
    if which.get("side") == "fix" or str((res.get("route") or {}).get("rule", "")).startswith(
            "steer:") or turn.status.startswith("steer_"):
        return "fix"
    return f"other:{turn.skill or turn.status}"


def run_case(world: Any, case: dict[str, Any]) -> dict[str, Any]:
    from mimi.api.agent import Agent
    from mimi.api.offline import OfflineModel

    engine = make_engine("sqlite://")
    ensure_schema(engine)
    seed_world(engine, world)
    _setup(engine, world, case)
    agent = Agent(engine, models=[OfflineModel()], router_model=None, lease_s=30.0,
                  clock=lambda: T0 + timedelta(minutes=30))
    phone = world.shop(SHOP).phone
    got: list[str] = []
    questions = 0
    for n, text in enumerate(case["replies"]):
        v = agent.submit(TENANT, Inbound(
            source="shop_message", org_id=TENANT, external_id=f"{case['id']}.{n + 1}",
            received_at=T0 + timedelta(minutes=2 * n),
            payload=Payload(text=text, from_phone=phone)))
        while agent.process() is not None:
            pass
        with engine.connect() as conn:
            t = conn.execute(sa.select(s.turns).where(s.turns.c.turn_id == v.turn_id)).one()
            out = conn.execute(sa.select(s.messages.c.text).where(
                s.messages.c.turn_id == v.turn_id, s.messages.c.direction == "out",
                s.messages.c.channel == "shop_whatsapp")).scalars().all()
        o = outcome_of(t)
        got.append(o)
        questions += int(o == "which")
        if o == "which" and not any("delivery receipt" in x and "change?" in x for x in out):
            got[-1] = "which:no_question_sent"
    with engine.connect() as conn:
        consent = conn.execute(sa.select(sa.func.count()).select_from(s.intents).where(
            s.intents.c.tool == "record_delivery_consent")).scalar()
        aprs = {r.approval_id: r.status for r in conn.execute(sa.select(
            s.approvals.c.approval_id, s.approvals.c.status))}
        state = loads(conn.execute(sa.select(s.threads.c.state).where(
            s.threads.c.thread_id == THREAD)).scalar(), {})
    expect = list(case["expect"])
    ok = got == expect
    checks: list[str] = []
    if consent:
        checks.append(f"consent recorded {consent}")  # never offline: the reader is a model
    if any(v not in ("pending", "superseded") for v in aprs.values()):
        checks.append(f"approval changed {aprs}")
    if expect[-1] in ("which", "owner"):
        if state.get("pending_skill") != "order/drop-confirmation":
            checks.append("the receipt ask was closed")
        if not state.get("fix"):
            checks.append("the correction question was closed")
    if questions > 1:
        checks.append(f"asked which {questions} times")
    ok = ok and not checks
    binds = sum(1 for e, g in zip(expect, got, strict=False)
                if e in ("which", "owner") and g in ("receipt", "fix"))
    return {"id": case["id"], "expect": expect, "got": got, "ok": ok, "checks": checks,
            "ambiguous_binds": binds, "note": case.get("note", "")}


def evaluate(cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    world = load_world()
    rows = [run_case(world, c) for c in (cases or load_cases())]
    n_ok = sum(r["ok"] for r in rows)
    binds = sum(r["ambiguous_binds"] for r in rows)
    by_group: dict[str, list[int]] = {}
    for r in rows:
        g = by_group.setdefault(r["id"][1], [0, 0])
        g[0] += int(r["ok"])
        g[1] += 1
    report = {"cases": len(rows), "correct": round(n_ok / len(rows), 4) if rows else 0.0,
              "n_correct": n_ok, "ambiguous_binds": binds, "groups": by_group,
              "misses": [r for r in rows if not r["ok"]], "pass_marks": PASS_MARKS}
    report["passed"] = (report["correct"] >= PASS_MARKS["correct"]
                        and binds <= PASS_MARKS["ambiguous_binds"])
    report["rows"] = rows
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.api.which_eval")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)
    t0 = time.perf_counter()
    r = evaluate()
    for row in r["misses"]:
        print(f"MISS {row['id']}: expected {row['expect']} got {row['got']} {row['checks']}")
    print(f"Two open asks: {r['n_correct']}/{r['cases']} correct ({r['correct']:.1%}), "
          f"ambiguous replies bound {r['ambiguous_binds']}, groups "
          f"{ {k: f'{a}/{b}' for k, a_b in r['groups'].items() for a, b in [a_b]} }, "
          f"{time.perf_counter() - t0:.1f}s")
    print(f"PASSED: {r['passed']}")
    if not args.no_write:
        REPORT.write_text(json.dumps({k: v for k, v in r.items()}, indent=1,
                                     sort_keys=True) + "\n")
    return 0 if r["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
