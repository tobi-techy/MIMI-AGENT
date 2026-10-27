"""Always-on runtime: durable jobs, leases, outbox, budgets, owner preemption.

Every test runs against both stores (in-memory and SQL on SQLite) because the
crash guarantees are a property of the store contract, not of one adapter.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.runtime import (
    Budget,
    IllegalTransition,
    InMemoryJobStore,
    Job,
    JobRunner,
    JobState,
    LeaseLost,
    OwnerMessage,
    StepContext,
    StepOutcome,
)
from mimi.runtime.outbox import RecordingIntentSink, idempotency_key, intent_types
from mimi.runtime.sql_store import SqlJobStore
from mimi.runtime.states import check_transition
from mimi.runtime.tracing import RecordingStepTracer
from mimi.types import ShopScope

T0 = datetime(2026, 10, 26, 16, 0, tzinfo=UTC)
SCOPE = ShopScope(tenant_id="coral-lane", shop_id="shop-chuks")


# Test-only intents. The runtime has no intent types of its own; a helper
# registers the proposals it may send, so these tests register three.
@dataclass(frozen=True)
class DraftReminder:
    scope: ShopScope
    counterparty_id: str
    text: str
    kind: str = field(default="draft_reminder")


@dataclass(frozen=True)
class RecordPaymentClaim:
    scope: ShopScope
    amount_kobo: int
    reference: str | None = None
    kind: str = field(default="record_payment_claim")


@dataclass(frozen=True)
class RecordStockCount:
    scope: ShopScope
    sku: str
    quantity: Decimal
    unit: str
    kind: str = field(default="record_stock_count")


TEST_INTENTS = intent_types(DraftReminder, RecordPaymentClaim, RecordStockCount)


class SimulatedCrash(BaseException):
    """The process dies: nothing in the runner may catch this."""


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Script:
    """A helper whose step N returns ``outcomes[N]`` and counts its calls."""

    def __init__(self, outcomes: list[Callable[[StepContext], StepOutcome]]):
        self.outcomes = outcomes
        self.calls: Counter[int] = Counter()
        self.seen: list[StepContext] = []

    def __call__(self, ctx: StepContext) -> StepOutcome:
        self.calls[ctx.step_index] += 1
        self.seen.append(ctx)
        return self.outcomes[ctx.step_index](ctx)


def cont(**kw: Any) -> Callable[[StepContext], StepOutcome]:
    return lambda ctx: StepOutcome(kind="continue", **kw)


def done(result: str = "ok") -> Callable[[StepContext], StepOutcome]:
    return lambda ctx: StepOutcome(kind="done", result=result)


def reminder(text: str) -> DraftReminder:
    return DraftReminder(scope=SCOPE, counterparty_id="shop-chuks", text=text)


class CrashingSink(RecordingIntentSink):
    """Dies on the Nth submit, before or after Go accepted it."""

    def __init__(self, crash_on: int, after_accept: bool = False) -> None:
        super().__init__()
        self.crash_on = crash_on
        self.after_accept = after_accept

    def submit(
        self, tenant_id: str, idempotency_key: str, kind: str, payload: dict[str, Any]
    ) -> str:
        if self.calls + 1 == self.crash_on:
            self.crash_on = -1
            if self.after_accept:
                super().submit(tenant_id, idempotency_key, kind, payload)
            else:
                self.calls += 1
            raise SimulatedCrash()
        return super().submit(tenant_id, idempotency_key, kind, payload)


@pytest.fixture(params=["memory", "sql"])
def store(request: pytest.FixtureRequest) -> Iterator[Any]:
    if request.param == "memory":
        yield InMemoryJobStore()
        return
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    sql = SqlJobStore(engine)
    sql.ensure_schema()
    yield sql
    engine.dispose()


def new_job(store: Any, **kw: Any) -> Job:
    job = Job(
        job_id=kw.pop("job_id", "job-1"),
        tenant_id="coral-lane",
        helper="collector",
        thread_id="thread-1",
        wake_source=kw.pop("wake_source", "owner_message"),
        goal="chase everyone over 50k",
        **kw,
    )
    store.create(job)
    return job


def runner(
    store: Any,
    script: Script,
    *,
    clock: Clock,
    sink: RecordingIntentSink | None = None,
    worker: str = "w1",
    tracer: RecordingStepTracer | None = None,
) -> JobRunner:
    return JobRunner(
        store=store,
        sink=sink if sink is not None else RecordingIntentSink(),
        tracer=tracer or RecordingStepTracer(),
        steps={"collector": script},
        worker_id=worker,
        lease_s=30,
        clock=clock,
        intent_types=TEST_INTENTS,
    )


# --------------------------------------------------------------- state machine


def test_transition_table_rejects_illegal_moves() -> None:
    check_transition(JobState.QUEUED, JobState.WORKING)
    check_transition(JobState.WORKING, JobState.SLEEPING_UNTIL)
    with pytest.raises(IllegalTransition):
        check_transition(JobState.DONE, JobState.WORKING)
    with pytest.raises(IllegalTransition):
        check_transition(JobState.QUEUED, JobState.DONE)
    with pytest.raises(IllegalTransition):
        check_transition(JobState.WAITING_ON_OWNER, JobState.WORKING)


def test_job_roundtrips_through_json() -> None:
    job = Job("j", "t", "collector", "th", "routine", "g", plan=["a"], todo=["b"])
    job.sleeping_until = T0
    assert Job.from_dict(job.to_dict()) == job


# ------------------------------------------------------------------ happy path


def test_runs_to_done_persisting_after_every_step(store: Any) -> None:
    clock = Clock()
    new_job(store)
    sink = RecordingIntentSink()
    tracer = RecordingStepTracer()
    script = Script(
        [
            cont(plan=("read balances", "draft reminders"), cost_kobo=120),
            cont(intents=(reminder("Hi Chuks, N54,000 is due"),), cost_kobo=300),
            done("1 reminder drafted"),
        ]
    )
    versions: list[int] = []
    real_save = store.save

    def spy(job: Job, worker: str, ack: tuple[str, ...] = ()) -> Job:
        saved: Job = real_save(job, worker, ack)
        versions.append(saved.step_index)
        return saved

    store.save = spy
    report = runner(store, script, clock=clock, sink=sink, tracer=tracer).run_once()

    assert report is not None and report.state is JobState.DONE
    assert report.steps_run == 3
    # A save landed after each of the three steps (plus outbox/settle saves).
    assert {1, 2, 3} <= set(versions)
    final = store.get("job-1")
    assert final.state is JobState.DONE and final.result == "1 reminder drafted"
    assert final.plan == ["read balances", "draft reminders"]
    assert final.spend.steps == 3 and final.spend.kobo == 420
    assert final.lease_owner is None
    assert len(sink.accepted) == 1
    _, kind, payload = next(iter(sink.accepted.values()))
    assert kind == "draft_reminder" and payload["text"].startswith("Hi Chuks")
    # One Langfuse-shaped span per step.
    assert [s.attrs["step_index"] for s in tracer.spans] == [0, 1, 2]
    assert tracer.spans[1].attrs["intents"] == 1
    assert tracer.spans[1].attrs["cost_kobo"] == 300


# ---------------------------------------------------------------- crash/resume


def test_crash_resume_never_redoes_a_saved_step_or_double_sends(store: Any) -> None:
    """Worker A dies mid-submit after step 1. Worker B takes over the lease."""
    clock = Clock()
    new_job(store)
    script = Script(
        [
            cont(),
            cont(
                intents=(
                    reminder("Hi Chuks, N54,000 is due"),
                    RecordPaymentClaim(scope=SCOPE, amount_kobo=5_400_000),
                )
            ),
            cont(),
            done(),
        ]
    )
    crashing = CrashingSink(crash_on=2, after_accept=True)
    with pytest.raises(SimulatedCrash):
        runner(store, script, clock=clock, sink=crashing, worker="A").run_once()

    mid = store.get("job-1")
    assert mid.state is JobState.WORKING and mid.lease_owner == "A"
    assert mid.step_index == 2  # step 1 was saved before anything left
    assert len(mid.pending_outbound) == 2

    # Before the lease expires nobody can take it.
    assert runner(store, script, clock=clock, worker="B").run_once() is None
    clock.advance(31)
    report = runner(store, script, clock=clock, sink=crashing, worker="B").run_once()

    assert report is not None and report.state is JobState.DONE
    assert script.calls == Counter({0: 1, 1: 1, 2: 1, 3: 1})
    assert len(crashing.accepted) == 2  # both intents, each exactly once
    # Both were resent after the takeover (at least once); Go's dedupe on the
    # key made the resends no-ops (exactly once).
    assert crashing.calls == 4
    final = store.get("job-1")
    assert final.pending_outbound == [] and len(final.sent_keys) == 2


def test_crash_inside_a_step_reruns_only_that_step(store: Any) -> None:
    clock = Clock()
    new_job(store)
    crashed = {"once": False}

    def flaky(ctx: StepContext) -> StepOutcome:
        if not crashed["once"]:
            crashed["once"] = True
            raise SimulatedCrash()
        return StepOutcome(kind="continue", intents=(reminder("due"),))

    script = Script([cont(intents=(reminder("first"),)), flaky, done()])
    sink = RecordingIntentSink()
    with pytest.raises(SimulatedCrash):
        runner(store, script, clock=clock, sink=sink, worker="A").run_once()
    clock.advance(31)
    report = runner(store, script, clock=clock, sink=sink, worker="B").run_once()

    assert report is not None and report.state is JobState.DONE
    assert script.calls == Counter({0: 1, 1: 2, 2: 1})
    assert sorted(p[2]["text"] for p in sink.accepted.values()) == ["due", "first"]


def test_crash_before_go_accepts_still_delivers_once(store: Any) -> None:
    clock = Clock()
    new_job(store)
    script = Script([cont(intents=(reminder("due"),)), done()])
    sink = CrashingSink(crash_on=1, after_accept=False)
    with pytest.raises(SimulatedCrash):
        runner(store, script, clock=clock, sink=sink, worker="A").run_once()
    assert sink.accepted == {}
    clock.advance(31)
    runner(store, script, clock=clock, sink=sink, worker="B").run_once()
    assert len(sink.accepted) == 1
    assert script.calls == Counter({0: 1, 1: 1})


# ---------------------------------------------------------------------- leases


def test_stale_worker_cannot_write_after_takeover(store: Any) -> None:
    clock = Clock()
    new_job(store)
    a = store.claim("A", clock(), 30)
    assert a is not None
    clock.advance(31)
    b = store.claim("B", clock(), 30)
    assert b is not None and b.lease_owner == "B"
    with pytest.raises(LeaseLost):
        store.heartbeat("job-1", "A", clock(), 30)
    a.step_index = 99
    with pytest.raises(LeaseLost):
        store.save(a, "A")
    assert store.get("job-1").step_index == 0


def test_heartbeat_keeps_the_lease(store: Any) -> None:
    clock = Clock()
    new_job(store)
    assert store.claim("A", clock(), 30) is not None
    for _ in range(5):
        clock.advance(20)
        store.heartbeat("job-1", "A", clock(), 30)
    assert store.claim("B", clock(), 30) is None


def test_long_job_heartbeats_between_steps(store: Any) -> None:
    """A job longer than its lease survives because every step renews it."""
    clock = Clock()
    new_job(store, budget=Budget(max_steps=100, max_seconds=10_000, max_kobo=10**9))

    def slow(ctx: StepContext) -> StepOutcome:
        clock.advance(20)  # each step is most of a lease
        return StepOutcome(kind="done" if ctx.step_index == 9 else "continue")

    script = Script([slow] * 10)
    report = runner(store, script, clock=clock).run_once()
    assert report is not None and report.state is JobState.DONE
    assert report.steps_run == 10 and not report.lease_lost


# --------------------------------------------------------------------- budgets


def test_step_budget_parks_and_asks_then_owner_yes_resumes(store: Any) -> None:
    clock = Clock()
    new_job(
        store,
        budget=Budget(max_steps=2, max_seconds=600, max_kobo=10_000),
        budget_grant=Budget(max_steps=2, max_seconds=0, max_kobo=0),
    )
    script = Script([cont(), cont(), cont(), done()])
    r = runner(store, script, clock=clock)
    report = r.run_once()
    assert report is not None and report.state is JobState.WAITING_ON_OWNER
    parked = store.get("job-1")
    assert (
        parked.waiting_reason == "budget: This job has taken 2 of 2 steps. Keep going?"
    )
    assert r.run_once() is None  # nothing runs while it waits

    store.post_owner_message("job-1", OwnerMessage("m1", "Yes", clock()))
    report = r.run_once()
    assert report is not None and report.state is JobState.DONE
    assert store.get("job-1").budget.max_steps == 4


def test_naira_budget_question_uses_code_figures(store: Any) -> None:
    clock = Clock()
    new_job(store, budget=Budget(max_steps=50, max_seconds=600, max_kobo=5_000))
    script = Script([cont(cost_kobo=3_000), cont(cost_kobo=2_500), done()])
    report = runner(store, script, clock=clock).run_once()
    assert report is not None and report.state is JobState.WAITING_ON_OWNER
    assert store.get("job-1").waiting_reason == (
        "budget: I have used N55.00 of the N50.00 allowed for this job. Keep going?"
    )


def test_time_budget_counts_working_seconds(store: Any) -> None:
    clock = Clock()
    new_job(store, budget=Budget(max_steps=50, max_seconds=30, max_kobo=10**6))

    def tick(ctx: StepContext) -> StepOutcome:
        clock.advance(16)
        return StepOutcome()

    report = runner(store, Script([tick] * 5), clock=clock).run_once()
    assert report is not None and report.steps_run == 2
    assert store.get("job-1").waiting_reason is not None


# ------------------------------------------------------------ owner preemption


def test_owner_message_is_taken_at_the_next_step_boundary(store: Any) -> None:
    clock = Clock()
    new_job(store, wake_source="routine")

    def step0(ctx: StepContext) -> StepOutcome:
        # Arrives while step 0 is running: it must not interrupt mid-step.
        store.post_owner_message(
            "job-1", OwnerMessage("m1", "skip Chuks, he paid cash", clock())
        )
        return StepOutcome()

    script = Script([step0, cont(), done()])
    runner(store, script, clock=clock).run_once()
    assert script.seen[0].new_owner_messages == ()
    assert script.seen[1].new_owner_messages == ("skip Chuks, he paid cash",)
    assert script.seen[2].new_owner_messages == ()  # taken once, acked
    assert store.pending_owner_messages("job-1") == []


def test_owner_stop_parks_before_the_next_step(store: Any) -> None:
    clock = Clock()
    new_job(store)

    def step0(ctx: StepContext) -> StepOutcome:
        store.post_owner_message("job-1", OwnerMessage("m1", "Stop!", clock()))
        return StepOutcome()

    script = Script([step0, cont(), done()])
    report = runner(store, script, clock=clock).run_once()
    assert report is not None and report.state is JobState.WAITING_ON_OWNER
    assert script.calls == Counter({0: 1})
    assert store.get("job-1").waiting_reason == "owner said stop"


def test_jobs_with_owner_messages_are_claimed_first(store: Any) -> None:
    clock = Clock()
    new_job(store, job_id="a-routine", wake_source="routine")
    new_job(store, job_id="b-owner")
    store.post_owner_message("b-owner", OwnerMessage("m1", "how far?", clock()))
    claimed = store.claim("w", clock(), 30)
    assert claimed is not None and claimed.job_id == "b-owner"


def test_duplicate_owner_message_is_taken_once(store: Any) -> None:
    clock = Clock()
    new_job(store)
    msg = OwnerMessage("wamid.1", "skip Chuks", clock())
    store.post_owner_message("job-1", msg)
    store.post_owner_message("job-1", msg)
    assert len(store.pending_owner_messages("job-1")) == 1


# ---------------------------------------------------------------------- sleeps


def test_sleeping_job_wakes_on_time_or_on_owner(store: Any) -> None:
    clock = Clock()
    new_job(store)
    script = Script(
        [
            lambda ctx: StepOutcome(
                kind="sleep", sleep_until=ctx.now + timedelta(hours=2)
            ),
            lambda ctx: StepOutcome(
                kind="sleep", sleep_until=ctx.now + timedelta(days=1)
            ),
            done(),
        ]
    )
    r = runner(store, script, clock=clock)
    assert r.run_once() is not None
    assert store.get("job-1").state is JobState.SLEEPING_UNTIL
    clock.advance(3600)
    assert r.run_once() is None
    clock.advance(3601)
    assert r.run_once() is not None  # timer woke it; it sleeps again for a day
    store.post_owner_message("job-1", OwnerMessage("m1", "any news?", clock()))
    report = r.run_once()  # owner woke it early
    assert report is not None and report.state is JobState.DONE


# ----------------------------------------------------------------- guardrails


def test_step_returning_a_non_intent_blocks_and_sends_nothing(store: Any) -> None:
    clock = Clock()
    new_job(store)

    class TransferFunds:  # what Mimi must never be able to emit
        kind = "transfer_funds"

    script = Script([lambda ctx: StepOutcome(intents=(TransferFunds(),))])  # type: ignore[arg-type]
    sink = RecordingIntentSink()
    report = runner(store, script, clock=clock, sink=sink).run_once()
    assert report is not None and report.state is JobState.BLOCKED
    assert sink.calls == 0
    assert "never act" in (store.get("job-1").waiting_reason or "")


def test_intent_for_another_tenant_is_refused(store: Any) -> None:
    clock = Clock()
    new_job(store)
    other = ShopScope(tenant_id="someone-else", shop_id="shop-chuks")
    bad = DraftReminder(scope=other, counterparty_id="x", text="hi")
    sink = RecordingIntentSink()
    report = runner(
        store, Script([lambda ctx: StepOutcome(intents=(bad,))]), clock=clock, sink=sink
    ).run_once()
    assert report is not None and report.state is JobState.BLOCKED
    assert sink.calls == 0


def test_three_failed_steps_block_the_job(store: Any) -> None:
    clock = Clock()
    new_job(store)

    def boom(ctx: StepContext) -> StepOutcome:
        raise RuntimeError("gateway 500")

    report = runner(store, Script([boom]), clock=clock).run_once()
    assert report is not None and report.state is JobState.BLOCKED
    assert store.get("job-1").failures == 3


def test_unknown_helper_blocks(store: Any) -> None:
    clock = Clock()
    new_job(store)
    r = JobRunner(
        store=store,
        sink=RecordingIntentSink(),
        tracer=RecordingStepTracer(),
        steps={},
        worker_id="w",
        clock=clock,
    )
    report = r.run_once()
    assert report is not None and report.state is JobState.BLOCKED


# ----------------------------------------------------------------- idempotency


def test_idempotency_key_is_stable_and_content_bound() -> None:
    payload = {"amount_kobo": 5_400_000, "reference": None}
    a = idempotency_key("job-1", 3, 0, "record_payment_claim", payload)
    assert a == idempotency_key("job-1", 3, 0, "record_payment_claim", dict(payload))
    assert a != idempotency_key("job-1", 4, 0, "record_payment_claim", payload)
    assert a != idempotency_key(
        "job-1", 3, 0, "record_payment_claim", {**payload, "amount_kobo": 1}
    )


def test_decimal_quantities_serialise_exactly() -> None:
    from mimi.runtime.outbox import intent_payload

    kind, payload = intent_payload(
        RecordStockCount(
            scope=SCOPE, sku="noodles-70g", quantity=Decimal("2.5"), unit="carton"
        ),
        TEST_INTENTS,
    )
    assert kind == "record_stock_count" and payload["quantity"] == "2.5"


def test_sql_claim_race_has_one_winner(tmp_path: Any) -> None:
    """Two workers read the same queued row; the version CAS lets one win."""
    engine = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'jobs.db'}")
    sql = SqlJobStore(engine)
    sql.ensure_schema()
    new_job(sql)
    stale = sql.get("job-1")
    first = sql.claim("A", T0, 30)
    assert first is not None
    stale.move(JobState.WORKING)
    stale.lease_owner = "B"
    stale.version += 1
    with engine.begin() as conn:
        assert not SqlJobStore._cas(conn, stale, stale.version - 1)
    assert sql.get("job-1").lease_owner == "A"
    assert sql.claim("B", T0, 30) is None
    engine.dispose()


def test_step_scratch_is_saved_with_the_step_and_handed_to_the_next(store: Any) -> None:
    """A helper's own state (the harness loop state) survives between steps."""
    clock = Clock()
    seen: list[dict[str, Any]] = []

    def step(ctx: StepContext) -> StepOutcome:
        seen.append(dict(ctx.scratch))
        n = int(ctx.scratch.get("n", 0)) + 1
        return StepOutcome(kind="done" if n == 3 else "continue", scratch={"n": n})

    new_job(store)
    r = JobRunner(store=store, sink=RecordingIntentSink(), tracer=RecordingStepTracer(),
                  steps={"collector": step}, worker_id="w1", clock=clock,
                  intent_types=TEST_INTENTS)
    assert r.run_once() is not None
    assert seen == [{}, {"n": 1}, {"n": 2}]
    assert store.get("job-1").scratch == {"n": 3}


def test_old_job_bodies_without_scratch_still_load() -> None:
    raw = Job(job_id="j", tenant_id="t", helper="h", thread_id="th", wake_source="routine",
              goal="g").to_dict()
    raw.pop("scratch")
    assert Job.from_dict(raw).scratch == {}
