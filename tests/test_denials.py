"""The denial circuit breaker (RAI-324): a stuck helper stops and asks the owner.

Three denials in a row in one turn park the turn (``stopped_denied``); ten
denials in the thread's last 50 finished-turn reviews trip it too. A reply
after the step's own refusal must ask for a next step, never claim success.
Everything is offline: scripted models, the in-process Go stand-in.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from typing import Any

import pytest

from mimi.harness import claims as C
from mimi.harness import denials as D
from mimi.harness.collector import collector_helper, default_registry
from mimi.harness.denials_eval import CASES, judge, main
from mimi.harness.gateway import Gateway, ScriptedModel, say
from mimi.harness.go import FakeGo
from mimi.harness.loop import Harness, LoopState
from mimi.harness.policy import GoPolicy
from mimi.harness.types import RunEvent
from mimi.world.loader import load_world

WORLD = load_world()
AT = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
TENANT = WORLD.tenant_id


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*a: Any, **k: Any) -> None:
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)


@pytest.mark.parametrize("case", CASES, ids=[c.cid for c in CASES])
def test_denials_case(case: Any) -> None:
    row = judge(case)
    assert row["ok"], row


def _run(*turns: Any, withheld: tuple[str, ...] = ()) -> Any:
    model = ScriptedModel(list(turns) + [say("nothing left to do")] * 4)
    go = FakeGo(WORLD, withheld={s: frozenset({"shop:*"}) for s in withheld})
    helper = collector_helper(default_registry(), GoPolicy())
    h = Harness(book=go, control=go, events=go, gateway=Gateway([model]), policy=GoPolicy(),
                clock=lambda: AT)
    ev = RunEvent(event_id="t", tenant_id=TENANT, thread_id="t:denial",
                  wake_source="owner_message", text="carry on", sender="owner", at=AT)
    state = h.start(ev, helper)
    r = None
    for _ in range(len(turns) + 4):
        res = h.step(state, helper)
        state = LoopState.model_validate(res.state)
        if res.finished:
            r = res.result
            break
    assert r is not None
    return r


def test_three_consecutive_denials_park_with_an_owner_question() -> None:
    r = _run(
        say("", ("get_balance", {"shop_id": "shop-001"})),
        say("", ("get_invoices", {"shop_id": "shop-001"})),
        say("", ("get_payment_history", {"shop_id": "shop-001"})),
        withheld=("shop-001",))
    assert r.status == "stopped_denied"
    tripped = [e for e in r.events if e.type == "breaker_tripped"]
    assert tripped and tripped[0].data["kind"] == "in_a_row"
    # Nothing dispatched after the trip: one parked question, no message draft.
    assert [d for d in r.outbound_drafts if d.kind != "question"] == []
    assert r.owner_question and "3 times in a row" in r.owner_question
    assert "Nothing more was sent" in r.owner_question
    # No em dashes in the owner note (house style).
    assert "\u2014" not in r.owner_question and "--" not in r.owner_question


def test_progress_resets_the_streak() -> None:
    # Two denials, then a progress step that does NOT end the run, then one
    # more denial: without the reset the third denial would trip the breaker.
    # The progress step must not end the turn, or the run would stop before
    # the final denial and the test would pass even with no reset.
    r = _run(
        say("", ("get_balance", {"shop_id": "shop-001"})),
        say("", ("get_payment_history", {"shop_id": "shop-001"})),
        say("", ("get_invoices", {"shop_id": "shop-002"})),
        say("", ("get_balance", {"shop_id": "shop-001"})),
        withheld=("shop-001",))
    assert r.status != "stopped_denied"


def test_window_rule_arms_then_needs_a_fresh_denial() -> None:
    """A heavy thread arms the breaker; a fresh denial fires it, but a clean
    turn still carries on, so the window is never a one way door."""
    def run(*calls: Any) -> Any:
        model = ScriptedModel([say("", *calls)])
        go = FakeGo(WORLD)
        helper = collector_helper(default_registry(), GoPolicy())
        h = Harness(book=go, control=go, events=go, gateway=Gateway([model]),
                    policy=GoPolicy(), clock=lambda: AT)
        ev = RunEvent(event_id="t", tenant_id=TENANT, thread_id="t:window",
                      wake_source="owner_message", text="carry on", sender="owner", at=AT)
        state = h.start(ev, helper)
        state.denial_window_denials, state.denial_window_size = (10, 50)
        state = LoopState.model_validate(state.model_dump())
        return h.step(state, helper)

    refused = run(("get_balance", {"shop_id": "shop-999"}))
    assert refused.finished is not None and refused.result.status == "stopped_denied"
    kinds = {str((e.data or {}).get("kind")) for e in refused.result.events
             if e.type == "breaker_tripped"}
    assert "window" in kinds
    clean = run(("get_balance", {"shop_id": "shop-001"}),
                ("send_to_user", {"channel": "owner_console",
                                  "body": "Mama Sola Provisions owes N3,497,300.",
                                  "ends_turn": True}))
    assert clean.finished is not None and clean.result.status != "stopped_denied"


def test_a_reply_after_a_refusal_must_ask_not_claim() -> None:
    msgs: list[dict[str, Any]] = [
        {"role": "assistant", "tool_calls": [{"id": "1"}]},
        {"role": "tool",
         "content": '{"denied":true,"reason":"no","next_step":"do not use it"}'},
    ]
    assert C.dishonest_success("I have sent the reminder.", messages=msgs, intents=[])
    assert C.dishonest_success("I have read and checked the balance.",
                               messages=msgs, intents=[])
    assert C.dishonest_success("The follow-up is scheduled and booked.",
                               messages=msgs, intents=[])
    # No refusal behind it: the gate stays quiet (the outcomes check judges it).
    ok: list[dict[str, Any]] = [
        {"role": "assistant", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": '{"ok": true, "data": {"a": 1}}'},
    ]
    assert C.dishonest_success("Mama Sola Provisions owes N3,497,300.",
                               messages=ok, intents=[]) == []


def test_denial_counter_rules() -> None:
    assert D.DenialCounter(in_a_row=3).tripped()
    assert not D.DenialCounter(in_a_row=2).tripped()
    # The window arms the breaker; the turn's own denial fires it.
    assert D.DenialCounter(in_a_row=1, window_denials=10, window_size=50).tripped()
    assert not D.DenialCounter(window_denials=10, window_size=50).tripped()
    assert not D.DenialCounter(in_a_row=1, window_denials=9, window_size=50).tripped()
    assert D.DenialCounter(unknown=True).tripped()  # fail closed
    assert D.count_window([True] * 60) == (50, 50)  # capped at the window
    # One rule for the loop and the API's review count.
    assert D.is_denial("claim_blocked", {})
    assert D.is_denial("tool_refused", {"reason": "consent"})
    assert not D.is_denial("tool_refused", {"reason": "not_allowed"})
    assert not D.is_denial("tool_result", {})


def test_env_knobs_move_the_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_DENIALS_IN_A_ROW", "5")
    monkeypatch.setenv("MIMI_DENIALS_MAX_IN_WINDOW", "12")
    assert D.in_a_row_limit() == 5 and D.max_in_window() == 12
    assert not D.DenialCounter(in_a_row=3).tripped()
    assert D.DenialCounter(in_a_row=5).tripped()


def test_non_denial_refusals_never_count() -> None:
    for reason in ("not_allowed", "connector_down", "shop_rate_limit",
                   "followup_dedupe", "followup_cap"):
        assert not D.is_denial_event(
            type("E", (), {"type": "tool_refused", "data": {"reason": reason}})())
    assert D.is_denial_event(
        type("E", (), {"type": "tool_refused",
                        "data": {"reason": "consent", "shop_id": "shop-001"}})())


def test_denials_eval_passes() -> None:
    assert main(["--no-write"]) == 0


# ------------------------------------------------- the honesty gate's scoping


def test_an_earlier_steps_refusal_is_not_this_steps_denial() -> None:
    """The honesty gate judges the step's own tools: a refusal in an earlier
    step never holds a later clean step's answer (the leak the review found)."""
    denied = '{"denied": true, "reason": "consent", "next_step": "ask the owner"}'
    ok = '{"ok": true, "data": {"a": 1}}'
    history: list[dict[str, Any]] = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "0"}]},
        {"role": "tool", "tool_call_id": "0", "content": denied},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": ok},
    ]
    assert C.dishonest_success("The payment check found no match yet.", messages=history) == []
    assert C.dishonest_success("I read your book and found one open invoice.",
                               messages=history) == []
    # The same claim in the step whose own tool was refused is still held.
    own_step: list[dict[str, Any]] = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": denied},
    ]
    assert C.dishonest_success("The payment check found no match yet.", messages=own_step)
    assert C.dishonest_success("I read your book and found one open invoice.",
                               messages=own_step)


def test_an_honest_failure_report_is_never_held() -> None:
    """A reply that negates the success word reports the failure honestly."""
    denied = '{"denied": true, "reason": "consent", "next_step": "ask the owner"}'
    msgs: list[dict[str, Any]] = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": denied},
    ]
    assert C.dishonest_success(
        "I could not read the balance; what should I do?", messages=msgs) == []
    assert C.dishonest_success(
        "The balance check failed, so nothing was read.", messages=msgs) == []
    # Naming the failure with the bare noun is honest too: "the read was
    # refused" never claims the work got done (review follow-up).
    assert C.dishonest_success(
        "I could not check that yet; the read was refused, "
        "and I need someone to look at it.", messages=msgs) == []
    assert C.dishonest_success(
        "The invoice read was refused, so I could not check it.",
        messages=msgs) == []
    # A question form does not excuse a claim that is still affirmative.
    assert C.dishonest_success("I read the balance; what next?", messages=msgs)
    assert C.dishonest_success("I have sent the reminder.", messages=msgs)


def test_a_refusal_since_last_progress_still_holds() -> None:
    """The breaker's own notion: a refusal in an earlier step of the same run
    holds a later affirmative claim until progress resets the streak, while a
    bare noun naming the failure still passes (review follow-up)."""
    denied = '{"denied": true, "reason": "consent", "next_step": "ask the owner"}'
    ok = '{"ok": true, "data": {"a": 1}}'
    history: list[dict[str, Any]] = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "0"}]},
        {"role": "tool", "tool_call_id": "0", "content": denied},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": ok},
    ]
    # Step scope alone stays quiet (thread history is never a denial)...
    assert C.dishonest_success("I have read and checked the balance.",
                               messages=history) == []
    # ...but the loop tells the gate a denial is on the streak, so the same
    # reply is held until progress resets it.
    assert C.dishonest_success("I have read and checked the balance.",
                               messages=history, refused=True)
    # ...and the honest report of that same earlier refusal still goes out.
    assert C.dishonest_success(
        "I could not check that yet; the read was refused, "
        "and I need someone to look at it.",
        messages=history, refused=True) == []


# ------------------------------------------------------------ loop safety


def _run_to_finish(*turns: Any, withheld: tuple[str, ...] = ()) -> tuple[Any, Any]:
    model = ScriptedModel(list(turns) + [say("nothing left to do")] * 4)
    go = FakeGo(WORLD, withheld={s: frozenset({"shop:*"}) for s in withheld})
    helper = collector_helper(default_registry(), GoPolicy())
    h = Harness(book=go, control=go, events=go, gateway=Gateway([model]), policy=GoPolicy(),
                clock=lambda: AT)
    ev = RunEvent(event_id="t", tenant_id=TENANT, thread_id="t:denial",
                  wake_source="owner_message", text="carry on", sender="owner", at=AT)
    state = h.start(ev, helper)
    r = None
    for _ in range(len(turns) + 4):
        res = h.step(state, helper)
        state = LoopState.model_validate(res.state)
        if res.finished:
            r = res.result
            break
    assert r is not None
    return r, state


def test_the_breaker_answers_every_tool_call_once() -> None:
    """A tripped step keeps one tool reply per call: the tripping call is
    answered by ``_dispatch``, the calls after it are answered as skipped, and
    nothing is appended twice (the resumed transcript stays valid)."""
    r, state = _run_to_finish(
        say("", ("get_balance", {"shop_id": "shop-001"}),
            ("get_invoices", {"shop_id": "shop-001"})),
        say("", ("get_payment_history", {"shop_id": "shop-001"}),
            ("get_balance", {"shop_id": "shop-002"}),
            ("get_invoices", {"shop_id": "shop-002"})),
        withheld=("shop-001",))
    assert r.status == "stopped_denied"
    asked = [c["id"] for m in state.messages if m.get("role") == "assistant"
             for c in (m.get("tool_calls") or [])]
    answered = [m["tool_call_id"] for m in state.messages if m.get("role") == "tool"]
    assert sorted(answered) == sorted(asked)


def test_reopen_clears_the_denial_streak_and_window() -> None:
    r, state = _run_to_finish(
        say("", ("get_balance", {"shop_id": "shop-001"})),
        say("", ("get_invoices", {"shop_id": "shop-001"})),
        say("", ("get_payment_history", {"shop_id": "shop-001"})),
        withheld=("shop-001",))
    assert r.status == "stopped_denied" and state.denials_in_a_row >= 3
    state.denial_window_denials, state.denial_window_size = (10, 50)
    state.denial_store_unknown = True
    reopened = Harness.reopen(state)
    assert reopened.finished is None
    assert reopened.denials_in_a_row == 0
    assert reopened.denial_window_size == 0 and reopened.denial_window_denials == 0
    assert reopened.denial_store_unknown is False


# ------------------------------------------------ the API's review store


def _denial_engine(store_url: str) -> Any:
    from mimi.store import ensure_schema, make_engine, seed_world

    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, WORLD)
    return engine


def _denial_agent(engine: Any) -> Any:
    from mimi.api.agent import Agent
    from mimi.api.offline import OfflineModel

    return Agent(engine, models=[OfflineModel()], router_model=None, lease_s=30.0)


def test_denial_reviews_count_landed_turns(store_url: str) -> None:
    from mimi.store import schema as s

    engine = _denial_engine(store_url)
    agent = _denial_agent(engine)
    thread = f"{TENANT}:owner"
    with engine.begin() as conn:
        for i, (state, status) in enumerate([
                ("done", ""), ("parked", "stopped_denied"),
                ("done", ""), ("queued", "")]):
            conn.execute(s.turns.insert().values(
                turn_id=f"t{i}", tenant_id=TENANT, thread_id=thread, state=state,
                status=status, created_at="2026-10-26T08:00:00+00:00", created_ts=float(i)))
        # t0 ended clean but a step in it was refused (a withdrawn claim).
        conn.execute(s.thread_events.insert().values(
            tenant_id=TENANT, event_id="e0", thread_id=thread, turn_id="t0",
            type="claim_blocked", data="{}", at="2026-10-26T08:00:00+00:00", ts=0.0))
        # A messaging rate cap is logged but never a denial.
        conn.execute(s.thread_events.insert().values(
            tenant_id=TENANT, event_id="e2", thread_id=thread, turn_id="t2",
            type="tool_refused", data='{"reason": "shop_rate_limit"}',
            at="2026-10-26T08:00:00+00:00", ts=2.0))
    # Landed turns only (the queued t3 is not a review yet): t0 and t1 denied.
    assert agent.denial_reviews(TENANT, thread) == (2, 3)


def test_denial_reviews_fail_closed_when_unreadable(store_url: str) -> None:
    import sqlalchemy as sa

    from mimi.store import schema as s

    engine = _denial_engine(store_url)
    agent = _denial_agent(engine)
    thread = f"{TENANT}:owner"
    with engine.begin() as conn:
        conn.execute(s.turns.insert().values(
            turn_id="t0", tenant_id=TENANT, thread_id=thread, state="done", status="",
            created_at="2026-10-26T08:00:00+00:00", created_ts=0.0))
        conn.execute(sa.text("DROP TABLE thread_events"))
    assert agent.denial_reviews(TENANT, thread) is None


def test_resume_parked_leaves_a_denied_park_alone(store_url: str) -> None:
    import sqlalchemy as sa

    from mimi.store import schema as s

    engine = _denial_engine(store_url)
    agent = _denial_agent(engine)
    with engine.begin() as conn:
        for i, status in enumerate(("parked_budget", "stopped_denied")):
            conn.execute(s.turns.insert().values(
                turn_id=f"p{i}", tenant_id=TENANT, thread_id=f"{TENANT}:owner",
                state="parked", status=status,
                created_at="2026-10-26T08:00:00+00:00", created_ts=float(i)))
        resumed = agent.resume_parked(conn, TENANT)
        states = {r.turn_id: r.state for r in conn.execute(sa.select(s.turns))}
    assert resumed == ["p0"] and states["p0"] == "leased" and states["p1"] == "parked"


def test_resume_denied_clears_the_loop_guard(store_url: str) -> None:
    import json

    import sqlalchemy as sa

    from mimi.store import schema as s

    engine = _denial_engine(store_url)
    agent = _denial_agent(engine)
    ck = {"loop": {"guard": "no_progress", "guard_detail": "x", "denials_in_a_row": 3,
                   "call_counts": {"a": 2}, "fail_counts": {"b": 1}, "stale_steps": 2,
                   "tool_errors": 1, "denial_store_unknown": True}}
    with engine.begin() as conn:
        conn.execute(s.turns.insert().values(
            turn_id="d0", tenant_id=TENANT, thread_id=f"{TENANT}:owner", state="parked",
            status="stopped_denied", checkpoint=json.dumps(ck), version=1,
            created_at="2026-10-26T08:00:00+00:00", created_ts=0.0))
        assert agent.resume_denied(conn, "d0") == ["d0"]
        row = conn.execute(sa.select(s.turns).where(s.turns.c.turn_id == "d0")).one()
    loop = json.loads(row.checkpoint)["loop"]
    assert row.state == "queued" and loop["guard"] == ""
    assert loop["denials_in_a_row"] == 0 and loop["stale_steps"] == 0
    assert loop["denial_store_unknown"] is False and loop["call_counts"] == {}


def test_each_denial_park_queues_its_own_approval(store_url: str) -> None:
    """The approval key carries the park's version: approving a park and
    tripping again on the resumed turn queues a fresh item, never a duplicate."""
    import sqlalchemy as sa

    from mimi.harness.router import RouteDecision
    from mimi.harness.types import RunResult
    from mimi.store import schema as s
    from mimi.store.queue import Claim

    engine = _denial_engine(store_url)
    agent = _denial_agent(engine)
    thread = f"{TENANT}:owner"
    d = RouteDecision(status="routed", helper="collector", skill="collector/balance",
                      confidence=1.0, layer="rule")
    state = LoopState(run_id="run1", job_id="d1", thread_id=thread, tenant_id=TENANT,
                      helper="collector", wake_source="owner_message", started_at=AT,
                      messages=[], denials_in_a_row=3)
    state.finished = RunResult(run_id="run1", job_id="d1", thread_id=thread,
                               helper="collector", wake_source="owner_message",
                               status="stopped_denied", reason="breaker")
    with engine.begin() as conn:
        conn.execute(s.turns.insert().values(
            turn_id="d1", tenant_id=TENANT, thread_id=thread, state="leased",
            lease_owner="w", version=0,
            created_at="2026-10-26T08:00:00+00:00", created_ts=0.0))
    agent._park_denied([Claim(turn_id="d1", tenant_id=TENANT, thread_id=thread,
                              worker_id="w", version=0, attempts=1, batch=(),
                              checkpoint={}, takeover=False)], {}, d, [], state)
    # The owner approved: the turn is leased again at the next version.
    with engine.begin() as conn:
        conn.execute(s.turns.update().where(s.turns.c.turn_id == "d1")
                     .values(state="leased", status="", lease_owner="w", version=1))
    agent._park_denied([Claim(turn_id="d1", tenant_id=TENANT, thread_id=thread,
                              worker_id="w", version=1, attempts=2, batch=(),
                              checkpoint={}, takeover=True)], {}, d, [], state)
    with engine.connect() as conn:
        approvals = conn.execute(sa.select(sa.func.count()).select_from(s.approvals).where(
            s.approvals.c.turn_id == "d1",
            s.approvals.c.tool == "review_denied_job")).scalar()
    assert approvals == 2
