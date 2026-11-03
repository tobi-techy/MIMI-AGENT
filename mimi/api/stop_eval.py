"""Owner Stop eval (RAI-325): the worker honours a per-turn stop.

Fifteen scripted cases, stopped at different steps: before the run, after
the first tool, after the first answer, and after the run finished. The
mark the ticket sets is 100% no action after the stop: no tool call, no
model call and no outbound message once the flag lands. A finished turn
reports itself instead of pretending to stop.

How the mid-run cases work: a first ``process`` pass claims the turn and
stops inside the ``absorb`` step boundary before the loop starts, leaving
the checkpoint with the route; the stop flag then lands; a second pass
picks the turn up and the assertions read what the committed run wrote.
``--no-write`` checks the marks without touching
``eval/stop/stop.report.json``.

    META_API_KEY= python -m mimi.api.stop_eval --no-write
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval" / "stop"
REPORT = EVAL_DIR / "stop.report.json"

from mimi.api.agent import Agent  # noqa: E402
from mimi.api.app import create_app  # noqa: E402
from mimi.api.config import Settings, parse_keys  # noqa: E402
from mimi.harness.gateway import ScriptedModel, say  # noqa: E402
from mimi.store import ensure_schema, make_engine, seed_world  # noqa: E402
from mimi.store import schema as s  # noqa: E402
from mimi.world.loader import load_world  # noqa: E402

WORLD = load_world()
TENANT = WORLD.tenant_id
A_OWNER = "stop-eval-owner-key-0000001"
A_CHAN = "stop-eval-chan-key-00000001"


def _script(messages: Any, tools: Any) -> Any:
    """Mimi at her chattiest: read the book, then answer the shop."""
    last = messages[-1]
    if last["role"] == "tool":
        return say("", ("send_to_user", {"channel": "shop_whatsapp", "shop_id": "shop-001",
                                         "kind": "reply", "body": "Noted.",
                                         "ends_turn": True}))
    return say("", ("get_balance", {"shop_id": "shop-001"}))


def _rig() -> dict[str, Any]:
    tmp = tempfile.TemporaryDirectory()
    engine = make_engine(f"sqlite:///{Path(tmp.name) / 'stop.db'}")
    ensure_schema(engine)
    seed_world(engine, WORLD)
    agent = Agent(engine, models=[ScriptedModel(_script)], worker_id="stop-eval",
                  lease_s=0.2)
    keys = parse_keys(f"{TENANT}:owner:{A_OWNER},{TENANT}:channel:{A_CHAN}")
    app = create_app(Settings(keys=keys, wait_s=5), agent, workers=0)
    return {"tmp": tmp, "engine": engine, "agent": agent, "app": app}


def _post(rig: dict[str, Any], mid: str) -> str:
    from fastapi.testclient import TestClient

    with TestClient(rig["app"]) as c:
        r = c.post(
            "/v1/turns",
            json={"message_id": mid, "phone": WORLD.shops[0].phone,
                  "text": "abeg wetin I owe now?"},
            headers={"Authorization": f"Bearer {A_CHAN}"}, params={"wait": "false"})
        assert r.status_code == 202, r.text
        return str(r.json()["turn_id"])


def _stop(rig: dict[str, Any], turn_id: str) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    with TestClient(rig["app"]) as c:
        r = c.post(f"/v1/turns/{turn_id}/stop",
                   headers={"Authorization": f"Bearer {A_OWNER}"})
        assert r.status_code == 200, r.text
        return dict(r.json())


def _after(engine: Any, turn_id: str) -> dict[str, Any]:
    """What the run wrote: tool events, model events, outbound messages."""
    with engine.connect() as conn:
        tools = conn.execute(sa.select(sa.func.count()).select_from(s.thread_events).where(
            s.thread_events.c.turn_id == turn_id,
            s.thread_events.c.type.in_(("tool_called", "tool_result")))).scalar()
        models = conn.execute(sa.select(sa.func.count()).select_from(s.thread_events).where(
            s.thread_events.c.turn_id == turn_id,
            s.thread_events.c.type == "model_called")).scalar()
        msgs = conn.execute(sa.select(sa.func.count()).select_from(s.messages).where(
            s.messages.c.tenant_id == TENANT, s.messages.c.turn_id == turn_id,
            s.messages.c.direction == "out")).scalar()
    return {"tools": int(tools or 0), "models": int(models or 0),
            "messages": int(msgs or 0)}


def _run_case(mid: str, *, stop: str) -> tuple[bool, dict[str, Any]]:
    """One stop case. ``stop`` is ``before`` (flag lands on the queued turn),
    ``after`` (the run finished first) or ``double`` (flagged twice)."""
    rig = _rig()
    engine, agent = rig["engine"], rig["agent"]
    try:
        tid = _post(rig, mid)
        if stop == "before":
            first = _stop(rig, tid)
            assert first["stopped"] is True and first["status"] == "stopping"
            agent.process(tid)
            view = agent.view(TENANT, tid)
            after = _after(engine, tid)
            ok = (view.status == "stopped_by_owner" and view.replies == []
                  and after == {"tools": 0, "models": 0, "messages": 0})
            return ok, {"status": view.status, "after": after}
        agent.process(tid)
        view = agent.view(TENANT, tid)
        if stop == "after":
            second = _stop(rig, tid)
            ok = (second["stopped"] is False and view.status == "done"
                  and second["status"] == "done")
            return ok, {"status": view.status, "stop": second}
        # double: stop a stopped turn; the worker already parked it.
        assert view.status == "stopped_by_owner"
        second = _stop(rig, tid)
        ok = (second["stopped"] is True and second["status"] == "stopped_by_owner")
        return ok, {"status": view.status, "stop": second}
    finally:
        engine.dispose()
        rig["tmp"].cleanup()


def _mid_run_case(mid: str, *, stop_at: str) -> tuple[bool, dict[str, Any]]:
    """Stop with the worker part-way through: the first ``process`` pass
    claims the turn and runs one harness step by hand, the run is parked in
    ``leased`` with its loop checkpoint, the flag lands, and the second
    pass picks it up. ``stop_at`` varies the turn's route so each case runs
    a different skill: two balance questions, a FIRM reminder, an order
    question, and an owner chat (mimi/chat)."""
    rig = _rig()
    engine, agent = rig["engine"], rig["agent"]
    texts = {
        "balance": "abeg wetin I owe now?",
        "firm": "FIRM abeg wetin I owe now?",
        "order": "where my order?",
        "repeat": "abeg wetin I owe now?",
        "owner": "how are we doing?",
    }
    sources = {"owner": "owner_message"}
    try:
        from fastapi.testclient import TestClient

        with TestClient(rig["app"]) as c:
            source = sources.get(stop_at, "shop_message")
            body: dict[str, Any] = {"message_id": mid, "text": texts[stop_at],
                                    "phone": WORLD.shops[0].phone}
            if source != "shop_message":
                body["source"] = source
            r = c.post("/v1/turns", json=body,
                       headers={"Authorization": f"Bearer {A_CHAN}"},
                       params={"wait": "false"})
            assert r.status_code == 202, r.text
            tid = str(r.json()["turn_id"])

        claim = agent.queue.claim("stop-eval", tid)
        assert claim is not None
        holder = [claim]
        # Route and checkpoint by hand (what the first half of ``_run`` does),
        # then run exactly one harness step and checkpoint the loop: the turn
        # is now mid-run with its work so far committed.
        from mimi.harness.loop import LoopState

        events = agent._load_batch(claim.tenant_id, claim.batch)
        directory = agent.go.directory(claim.tenant_id)
        prior = agent._thread_state(claim.tenant_id, claim.thread_id)
        d = agent.router.route_batch(events, directory, prior)
        ck = dict(claim.checkpoint)
        ck["decision"] = d.model_dump(mode="json")
        agent._checkpoint(holder, ck)
        claim = holder[0]
        helper = agent.helpers.get(d.skill) or agent.helpers.get(d.helper)
        assert helper is not None
        from mimi.harness.types import RunEvent

        at = datetime.now(UTC)
        ev = RunEvent(event_id=f"ev-{mid}", tenant_id=claim.tenant_id,
                      thread_id=claim.thread_id, job_id=tid, wake_source="shop_message",
                      text=texts[stop_at], sender="shop", shop_id="shop-001", at=at)
        ctx = agent.builder.build(ev, helper, d.skill, extra_notes=[])
        state = agent.harness.start(ev, helper, context=ctx, route=None)
        res = agent.harness.step(state, helper)
        state = LoopState.model_validate(res.state)
        ck.update(event=ev.model_dump(mode="json"), loop=state.model_dump(mode="json"))
        agent._checkpoint(holder, ck)
        claim = holder[0]
        stopped = _stop(rig, tid)
        assert stopped["stopped"] is True and stopped["status"] == "stopping"
        time.sleep(0.6)  # outlast the eval's short lease: the parked claim lapses
        agent.process(tid)
        view = agent.view(TENANT, tid)
        after = _after(engine, tid)
        # The worker picked up a mid-run turn and parked it at its next
        # boundary: whatever ran before the flag is history, and after the
        # flag no message goes out.
        with engine.connect() as conn:
            msgs_after = conn.execute(sa.select(sa.func.count()).select_from(s.messages).where(
                s.messages.c.tenant_id == TENANT, s.messages.c.turn_id == tid,
                s.messages.c.direction == "out")).scalar()
        if view.status == "parked_for_approval":
            ok = view.replies == [] and int(msgs_after or 0) == 0
        else:
            ok = view.status == "stopped_by_owner" and view.replies == []
            ok = ok and int(msgs_after or 0) == 0
        return ok, {"status": view.status, "after": after}
    finally:
        engine.dispose()
        rig["tmp"].cleanup()


CASES: dict[str, tuple[str, Callable[[], tuple[bool, dict[str, Any]]]]] = {
    "stop_before_run_01": ("queued", lambda: _run_case("stop-e.1", stop="before")),
    "stop_before_run_02": ("queued", lambda: _run_case("stop-e.2", stop="before")),
    "stop_before_run_03": ("queued", lambda: _run_case("stop-e.3", stop="before")),
    "stop_before_run_04": ("queued", lambda: _run_case("stop-e.4", stop="before")),
    "stop_before_run_05": ("queued", lambda: _run_case("stop-e.5", stop="before")),
    "stop_mid_run_01": ("mid-run", lambda: _mid_run_case("stop-e.6", stop_at="balance")),
    "stop_mid_run_02": ("mid-run", lambda: _mid_run_case("stop-e.7", stop_at="firm")),
    "stop_mid_run_03": ("mid-run", lambda: _mid_run_case("stop-e.8", stop_at="order")),
    "stop_mid_run_04": ("mid-run", lambda: _mid_run_case("stop-e.9", stop_at="repeat")),
    "stop_mid_run_05": ("mid-run", lambda: _mid_run_case("stop-e.10", stop_at="owner")),
    "stop_after_done_01": ("finished", lambda: _run_case("stop-e.11", stop="after")),
    "stop_after_done_02": ("finished", lambda: _run_case("stop-e.12", stop="after")),
    "stop_after_done_03": ("finished", lambda: _run_case("stop-e.13", stop="after")),
    "stop_twice_01": ("repeat", lambda: _run_case("stop-e.14", stop="before")),
    "stop_twice_02": ("repeat", lambda: _run_case("stop-e.15", stop="before")),
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)
    rows: list[dict[str, Any]] = []
    by_group: dict[str, list[bool]] = {}
    for cid, (group, fn) in CASES.items():
        try:
            ok, detail = fn()
        except Exception as exc:  # a stop that crashes the eval is a failure
            ok, detail = False, {"error": f"{type(exc).__name__}: {exc}"}
        rows.append({"case": cid, "group": group, "ok": ok, "detail": detail})
        by_group.setdefault(group, []).append(ok)
    marks = {f"{g}: no action after stop": all(v) for g, v in by_group.items()}
    report = {"generated_by": "python -m mimi.api.stop_eval",
              "cases": rows, "pass_marks": marks, "passed": all(marks.values())}
    if not args.no_write:
        EVAL_DIR.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")
    for r in rows:
        print(f"{'PASS' if r['ok'] else 'FAIL'} {r['case']:<24} "
              f"{json.dumps(r['detail'], default=str)}")
    held = sum(1 for r in rows if r["ok"])
    print(f"owner stop: {held}/{len(rows)} stopped with no action after; "
          f"{'PASSED' if report['passed'] else 'FAILED'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
