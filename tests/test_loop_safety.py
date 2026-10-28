"""Loop safety: Mimi's agent loop cannot run away (RAI loop-safety parent).

Each test was written first and failed on main (02023d4) before its guard
existed; ``docs/LOOP-SAFETY.md`` has the audit and the before/after table.
Everything is offline: scripted models, the in-process Go stand-in, the
in-memory job store and a SQLite turn queue.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mimi.harness import (
    FakeGo,
    Gateway,
    GoPolicy,
    Harness,
    RunBudget,
    RunEvent,
    ScriptedModel,
    collector_helper,
    default_registry,
    say,
)
from mimi.harness.loop import LoopState
from mimi.runtime import InMemoryJobStore, Job, JobRunner, JobState, StepOutcome
from mimi.runtime.outbox import RecordingIntentSink
from mimi.runtime.tracing import RecordingStepTracer
from mimi.world.loader import load_world

WORLD = load_world()
AT = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
TENANT = WORLD.tenant_id


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*a: Any, **k: Any) -> None:
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)


def rig(turns: Any, *, budget: RunBudget | None = None, go: FakeGo | None = None,
        **go_kw: Any) -> tuple[Harness, Any, FakeGo, ScriptedModel]:
    go = go or FakeGo(WORLD, **go_kw)
    policy = GoPolicy()
    helper = collector_helper(default_registry(), policy, budget=budget)
    model = turns if isinstance(turns, ScriptedModel) else ScriptedModel(turns)
    h = Harness(book=go, control=go, events=go, gateway=Gateway([model]), policy=policy,
                clock=lambda: AT)
    return h, helper, go, model


def ev(text: str = "how is shop-001", *, wake: str = "owner_message", sender: str = "owner",
       shop: str = "", eid: str = "e1") -> RunEvent:
    return RunEvent(event_id=eid, tenant_id=TENANT, thread_id="thread-1",
                    wake_source=wake, text=text, sender=sender,  # type: ignore[arg-type]
                    shop_id=shop, at=AT)


def owner_notes(r: Any) -> list[str]:
    return [d.body for d in r.outbound_drafts if d.channel == "owner_console"]


SHOPS = [s.shop_id for s in WORLD.shops]


# ---------------------------------------------------------------- 1. doom loop


def test_same_call_repeated_stops_the_run_with_an_owner_note_and_no_more_spend() -> None:
    loop = ScriptedModel(lambda m, t: say("", ("get_balance", {"shop_id": "shop-001"}),
                                          cost_kobo=100))
    h, helper, _, model = rig(loop)
    r = h.run(ev(), helper)
    assert r.status == "stopped_runaway"
    # Three identical calls run; the fourth is never dispatched and no model
    # call follows it.
    assert model.calls == 4 and r.spend.steps == 4
    assert sum(1 for e in r.events if e.type == "tool_called") == 3
    notes = owner_notes(r)
    assert len(notes) == 1 and "same step" in notes[0] and "get_balance" in notes[0]
    assert any(e.type == "loop_guard" and e.data.get("guard") == "repeat" for e in r.events)


def test_same_failing_call_stops_sooner() -> None:
    # A shop the distributor does not have: Go refuses every time.
    loop = ScriptedModel(lambda m, t: say("", ("get_balance", {"shop_id": "shop-999"})))
    h, helper, _, model = rig(loop)
    r = h.run(ev(), helper)
    assert r.status == "stopped_runaway"
    assert model.calls == 3  # failed twice; the third try is stopped
    assert any(e.type == "loop_guard" and e.data.get("guard") == "repeat_failure"
               for e in r.events)


# ------------------------------------------------------------- 2. no progress


def test_steps_with_no_new_result_stop_the_run() -> None:
    # Alternating two calls never repeats either one four times, but after the
    # first two results nothing new comes back.
    calls = [("get_balance", {"shop_id": "shop-001"}),
             ("get_payment_history", {"shop_id": "shop-001"})]
    n = iter(range(100))
    loop = ScriptedModel(lambda m, t: say("", calls[next(n) % 2]))
    h, helper, _, model = rig(loop)
    r = h.run(ev(), helper)
    assert r.status == "stopped_runaway"
    assert any(e.type == "loop_guard" and e.data.get("guard") == "no_progress"
               for e in r.events)
    assert r.spend.steps <= 2 + 4  # two useful steps, then four without progress


def test_flapping_tool_stops_on_tool_errors() -> None:
    go = FakeGo(WORLD)
    n = {"i": 0}

    def flap(m: Any, t: Any) -> Any:
        n["i"] += 1
        # The bank connector goes up and down; the model keeps trying new
        # amounts, so no call repeats.
        if n["i"] % 2:
            go.down.add((TENANT, "bank_feed"))
        else:
            go.down.discard((TENANT, "bank_feed"))
        return say("", ("check_payment", {"shop_id": "shop-001",
                                          "amount_kobo": 100_000 + n["i"]}))

    h, helper, _, model = rig(ScriptedModel(flap), go=go)
    r = h.run(ev(), helper)
    assert r.status == "stopped_runaway", r.reason
    assert r.spend.steps < 12


# ------------------------------------------------------- 3. helper ping-pong


def test_ask_helper_back_up_the_chain_is_refused() -> None:
    turns = [say("", ("ask_helper", {"helper": "proof", "shop_id": "shop-001",
                                     "task": "check this screenshot again"})),
             say("", ("send_to_user", {"channel": "owner_console", "body": "Asked the owner.",
                                       "ends_turn": True}))]
    h, helper, _, _ = rig(turns)
    state = h.start(ev(), helper, lineage=["proof", "collector"])
    while state.finished is None:
        state = LoopState.model_validate(h.step(state, helper).state)
    assert not [i for i in state.finished.intents if i.tool == "ask_helper"]
    assert any(e.type == "tool_refused" and e.data.get("reason") == "ping_pong"
               for e in state.finished.events)


def test_delegation_depth_is_capped() -> None:
    turns = [say("", ("ask_helper", {"helper": "order", "shop_id": "shop-001",
                                     "task": "check the coming delivery"})),
             say("", ("send_to_user", {"channel": "owner_console", "body": "Asked the owner.",
                                       "ends_turn": True}))]
    h, helper, _, _ = rig(turns)
    state = h.start(ev(), helper, lineage=["proof", "credit", "collector"])
    while state.finished is None:
        state = LoopState.model_validate(h.step(state, helper).state)
    assert not [i for i in state.finished.intents if i.tool == "ask_helper"]
    assert any(e.type == "tool_refused" and e.data.get("reason") == "delegation_depth"
               for e in state.finished.events)


def _job(job_id: str, **kw: Any) -> Job:
    return Job(job_id=job_id, tenant_id=TENANT, helper="h", thread_id="t", wake_source="routine",
               goal="g", **kw)


class Clock:
    def __init__(self) -> None:
        self.now = AT

    def __call__(self) -> datetime:
        return self.now


def test_child_job_shares_the_root_budget_and_cannot_reset_it() -> None:
    from mimi.runtime.budgets import Budget
    from mimi.runtime.delegation import DelegationRefused, spawn_child

    store = InMemoryJobStore()
    root = _job("root", budget=Budget(max_steps=4, max_seconds=60, max_kobo=1_000))
    root.helper = "collector"
    store.create(root)
    clock = Clock()

    def spend(ctx: Any) -> StepOutcome:
        return StepOutcome(cost_kobo=300)

    r = JobRunner(store=store, sink=RecordingIntentSink(), tracer=RecordingStepTracer(),
                  steps={"proof": spend, "collector": spend}, worker_id="w", clock=clock)
    # The root has spent N6 of its N10, then hands work to Proof.
    store._jobs["root"].spend.kobo = 600
    store._jobs["root"].state = JobState.WAITING_ON_OWNER
    child = spawn_child(store, store.get("root"), helper="proof", goal="check the alert",
                        job_id="child", now=clock())
    assert child.root_job_id == "root" and child.chain == ["collector", "proof"]
    assert child.budget.max_kobo == 400  # what the tree has left, not a fresh N50
    r.run_once()
    c = store.get("child")
    assert c.state is JobState.WAITING_ON_OWNER and c.spend.kobo <= 600
    assert "budget" in (c.waiting_reason or "")
    # The owner's "keep going" on the child does not grant the child anything.
    assert c.budget_grant.max_kobo == 0
    # Handing it back to Collector (A to B to A) is refused, and so is depth.
    with pytest.raises(DelegationRefused):
        spawn_child(store, c, helper="collector", goal="again", job_id="gc", now=clock())
    with pytest.raises(DelegationRefused):
        spawn_child(store, c, helper="credit", goal="deeper", job_id="gc2", now=clock(),
                    max_depth=1)


# --------------------------------------------------------- 4. self-retrigger


def test_a_job_that_keeps_waking_itself_is_capped_per_day() -> None:
    from mimi.runtime.budgets import Budget

    store = InMemoryJobStore()
    # A big step budget, so the self-wake cap (not the budget) is what stops it.
    store.create(_job("routine-1", budget=Budget(max_steps=1000, max_seconds=1e6,
                                                 max_kobo=10**9)))
    clock = Clock()

    def again(ctx: Any) -> StepOutcome:
        # Wakes itself one second later, forever.
        return StepOutcome(kind="sleep", sleep_until=ctx.now + timedelta(seconds=1))

    r = JobRunner(store=store, sink=RecordingIntentSink(), tracer=RecordingStepTracer(),
                  steps={"h": again}, worker_id="w", clock=clock)
    from mimi.runtime.runner import MAX_SELF_WAKES_PER_DAY

    runs = 0
    for _ in range(200):
        clock.now += timedelta(minutes=1)
        if r.run_once() is not None:
            runs += 1
    job = store.get("routine-1")
    assert job.state is JobState.WAITING_ON_OWNER, job.state
    assert runs == MAX_SELF_WAKES_PER_DAY + 1
    assert "woke itself" in (job.waiting_reason or "")


def test_a_wake_in_the_past_is_not_an_instant_loop() -> None:
    store = InMemoryJobStore()
    store.create(_job("routine-2"))
    clock = Clock()

    def past(ctx: Any) -> StepOutcome:
        return StepOutcome(kind="sleep", sleep_until=ctx.now - timedelta(hours=1))

    r = JobRunner(store=store, sink=RecordingIntentSink(), tracer=RecordingStepTracer(),
                  steps={"h": past}, worker_id="w", clock=clock)
    r.run_once()
    assert r.run_once() is None  # not claimable again at the same instant
    assert store.get("routine-2").sleeping_until > clock.now


def test_followups_per_shop_per_day_are_capped_and_deduped_in_a_run() -> None:
    def follow(i: int) -> Any:
        return ("set_followup", {"shop_id": "shop-005", "in_hours": 24 + i,
                                 "reason": f"promise {i}"})

    turns = [say("", follow(0), follow(1)),
             say("", ("send_to_user", {"channel": "owner_console", "body": "Noted.",
                                       "ends_turn": True}))]
    h, helper, go, _ = rig(turns)
    r = h.run(ev(), helper)
    assert [i.tool for i in r.intents].count("set_followup") == 1
    # Across runs: the day's cap.
    go2 = FakeGo(WORLD)
    total = 0
    for n in range(6):
        t = [say("", follow(10 + n)),
             say("", ("send_to_user", {"channel": "owner_console", "body": "Noted.",
                                       "ends_turn": True}))]
        h2, helper2, _, _ = rig(t, go=go2)
        rr = h2.run(ev(eid=f"f{n}"), helper2)
        for i in rr.intents:
            if i.tool == "set_followup":
                go2.submit(TENANT, i.idempotency_key, "harness_intent",
                           {"record": i.model_dump(mode="json")})
                total += 1
    assert total == 3


def test_outbound_to_one_shop_is_rate_limited() -> None:
    def send(i: int) -> Any:
        return ("send_to_user", {"channel": "shop_whatsapp", "shop_id": "shop-005",
                                 "body": f"Hello, update number {i}."})

    loop = ScriptedModel(lambda m, t: say("", send(len(m))))
    h, helper, _, _ = rig(loop)
    r = h.run(ev("hi", wake="shop_message", sender="shop", shop="shop-005"), helper)
    to_shop = [d for d in r.outbound_drafts if d.channel == "shop_whatsapp"]
    assert len(to_shop) == 3
    assert any(e.type == "tool_refused" and e.data.get("reason") == "shop_rate_limit"
               for e in r.events)


# ---------------------------------------------------------- 5. retry loops


def test_failed_steps_back_off_and_a_poison_job_is_parked() -> None:
    store = InMemoryJobStore()
    store.create(_job("poison"))
    clock = Clock()
    waits: list[float] = []

    def boom(ctx: Any) -> StepOutcome:
        raise RuntimeError("bad payload")

    r = JobRunner(store=store, sink=RecordingIntentSink(), tracer=RecordingStepTracer(),
                  steps={"h": boom}, worker_id="w", clock=clock, sleep=waits.append)
    r.run_once()
    assert store.get("poison").state is JobState.BLOCKED
    assert waits and waits == sorted(waits) and waits[0] > 0  # backoff grows
    # A human (or a bug) requeues it again and again: it is parked for good
    # after the lifetime cap, never retried forever.
    for _ in range(5):
        store._jobs["poison"].state = JobState.QUEUED
        store._jobs["poison"].failures = 0
        r.run_once()
    job = store.get("poison")
    assert job.state is JobState.BLOCKED and "poison" in (job.waiting_reason or "")
    assert job.total_failures <= 6


def test_a_job_that_kills_its_worker_is_parked_after_three_takeovers() -> None:
    store = InMemoryJobStore()
    store.create(_job("crash"))
    clock = Clock()
    for i in range(6):
        got = store.claim(f"w{i}", clock(), 30)  # the worker dies mid-step
        clock.now += timedelta(seconds=31)
        if got is None:
            break
    r = JobRunner(store=store, sink=RecordingIntentSink(), tracer=RecordingStepTracer(),
                  steps={"h": lambda ctx: StepOutcome(kind="done")}, worker_id="w9",
                  clock=clock)
    r.run_once()
    job = store.get("crash")
    assert job.state is JobState.BLOCKED and "poison" in (job.waiting_reason or "")


# ------------------------------------------------- 6. parked runs, no fresh budget


def test_a_bigger_helper_config_is_not_a_grant() -> None:
    loop = ScriptedModel(lambda m, t: say("", ("get_balance", {"shop_id": SHOPS[len(m) % 9]})))
    h, helper, _, model = rig(loop, budget=RunBudget(max_steps=3))
    state = h.start(ev(), helper)
    while state.finished is None:
        state = LoopState.model_validate(h.step(state, helper).state)
    assert state.finished.status == "stopped_budget" and model.calls == 3
    bigger = collector_helper(default_registry(), GoPolicy(), budget=RunBudget(max_steps=30))
    again = h.resume(state, bigger)
    assert again.status == "stopped_budget" and model.calls == 3  # nothing ran
    granted = h.resume(state, helper, grant=RunBudget(max_steps=2, max_seconds=0, max_kobo=0))
    assert granted.status == "stopped_budget" and model.calls == 5


def test_a_failed_or_runaway_run_does_not_reopen_without_a_grant() -> None:
    loop = ScriptedModel(lambda m, t: say("", ("get_balance", {"shop_id": "shop-001"})))
    h, helper, _, model = rig(loop)
    state = h.start(ev(), helper)
    while state.finished is None:
        state = LoopState.model_validate(h.step(state, helper).state)
    assert state.finished.status == "stopped_runaway"
    calls = model.calls
    reopened = h.reopen(state)
    assert reopened.finished is not None and model.calls == calls


def test_a_turn_that_keeps_killing_its_worker_is_poisoned_once(tmp_path: Any) -> None:
    import sqlalchemy as sa

    from mimi.api.agent import Agent
    from mimi.api.offline import OfflineModel
    from mimi.harness.inbound import Inbound, Payload
    from mimi.store import schema as s
    from mimi.store.db import ensure_schema, make_engine
    from mimi.store.queue import MAX_CLAIMS, TurnQueue
    from mimi.store.seed import seed_world

    engine = make_engine(f"sqlite:///{tmp_path / 'q.db'}")
    ensure_schema(engine)
    seed_world(engine, WORLD)
    agent = Agent(engine, models=[OfflineModel()])
    tid = agent.submit(TENANT, Inbound(
        source="shop_message", org_id=TENANT, external_id="poison-1",
        received_at=datetime.now(UTC),
        payload=Payload(text="hi", from_phone=WORLD.shops[0].phone))).turn_id
    now = [1_000_000.0]
    q = TurnQueue(engine, lease_s=30, clock=lambda: now[0])
    claims = 0
    for i in range(20):  # every holder dies mid-run; the lease lapses
        if q.claim(f"w{i}") is not None:
            claims += 1
        now[0] += 31
    row = q.get(tid)
    assert claims == MAX_CLAIMS and row.state == "failed" and row.status == "poisoned"
    with engine.connect() as conn:
        notes = conn.execute(sa.select(s.messages.c.text).where(
            s.messages.c.kind == "poison_note")).scalars().all()
    assert len(notes) == 1 and "parked" in notes[0]


def test_the_loop_safety_eval_passes() -> None:
    from mimi.api.loop_safety_eval import main

    assert main(["--no-write"]) == 0
