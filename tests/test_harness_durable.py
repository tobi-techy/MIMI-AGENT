"""The harness as durable job steps (RAI-278 x RAI-297): crash, resume, caps, idle.

Workers are thrown away between runs on purpose: the only state is the job
store, so every test that "kills" a worker builds a brand-new harness, model
client and runner and checks nothing was lost or done twice.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mimi.harness import FakeGo, Gateway, GoPolicy, Harness, collector_helper, default_registry, say
from mimi.harness.durable import HARNESS_INTENTS, harness_steps
from mimi.harness.eval import idle_suite, replay_suite, security_suite
from mimi.harness.gateway import ModelTurn
from mimi.harness.scenarios import build
from mimi.runtime import InMemoryJobStore, Job, JobRunner, JobState, OwnerMessage
from mimi.runtime.steps import StepContext, StepOutcome
from mimi.runtime.tracing import RecordingStepTracer
from mimi.world.loader import load_world

WORLD = load_world()
T0 = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
TENANT = WORLD.tenant_id
INV = next(r.delivery_id for r in WORLD.receivables
           if r.shop_id == "shop-005" and r.status == "overdue")


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*a: Any, **k: Any) -> None:
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)


class Crash(BaseException):
    """The worker process dies; nothing in the runner may catch this."""


SCRIPT: list[ModelTurn] = [
    say("", ("update_plan", {"plan": ["read balance", "remind", "tell rep", "report"],
                             "todo": ["remind shop-005"], "note": "chasing shop-005"})),
    say("", ("get_balance", {"shop_id": "shop-005"})),
    say("", ("send_reminder", {"shop_id": "shop-005", "invoice_id": INV, "tone": "soft"})),
    say("", ("notify_rep", {"shop_id": "shop-005", "message": "Reminder sent; please visit"})),
    say("", ("send_to_user", {"channel": "owner_console", "body": "Reminder queued for "
                              "shop-005 and the rep told.", "ends_turn": True})),
]


class TranscriptModel:
    """Answers by how many assistant turns the transcript already has, so a
    brand-new client (after a crash) carries on exactly where the job is."""

    def __init__(self, script: Sequence[ModelTurn]) -> None:
        self.name = "scripted"
        self.script = list(script)
        self.calls = 0
        self.seen: list[list[dict[str, Any]]] = []

    def chat(self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]], *,
             max_tokens: int) -> ModelTurn:
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        n = sum(1 for m in messages if m.get("role") == "assistant")
        return self.script[min(n, len(self.script) - 1)]


class World:
    """One store and one Go; workers come and go."""

    def __init__(self, **go_kw: Any) -> None:
        self.go = FakeGo(WORLD, **go_kw)
        self.store = InMemoryJobStore()
        self.now = T0
        self.models: list[TranscriptModel] = []
        self.tracer = RecordingStepTracer()

    def job(self, job_id: str = "job-1", goal: str = "chase shop-005",
            wake: str = "owner_message") -> None:
        self.store.create(Job(job_id=job_id, tenant_id=TENANT, helper="collector",
                              thread_id=f"thread-{job_id}", wake_source=wake, goal=goal))

    def worker(self, name: str, script: Sequence[ModelTurn] = SCRIPT,
               crash_at: int | None = None) -> JobRunner:
        model = TranscriptModel(script)
        self.models.append(model)
        pol = GoPolicy()
        h = Harness(book=self.go, control=self.go, events=self.go, gateway=Gateway([model]),
                    policy=pol, clock=lambda: self.now)
        steps = harness_steps(h, {"collector": collector_helper(default_registry(), pol)})
        if crash_at is not None:
            inner = steps["collector"]

            def crashing(ctx: StepContext) -> StepOutcome:
                if ctx.step_index == crash_at:
                    raise Crash()
                return inner(ctx)

            steps = {"collector": crashing}
        return JobRunner(store=self.store, sink=self.go, tracer=self.tracer, steps=steps,
                         worker_id=name, lease_s=30, clock=lambda: self.now,
                         intent_types=HARNESS_INTENTS)


def test_a_job_stopped_after_step_4_resumes_at_step_5_with_the_same_plan_and_no_repeats() -> None:
    w = World()
    w.job()
    with pytest.raises(Crash):
        w.worker("w1", crash_at=4).run_once()  # steps 0..3 saved, then the worker dies
    saved = w.store.get("job-1")
    assert saved.step_index == 4 and saved.state is JobState.WORKING
    assert saved.plan == ["read balance", "remind", "tell rep", "report"]
    applied_before = list(w.go.applied)
    reads_before = w.go.reads

    w.now += timedelta(seconds=31)  # the lease expires; a fresh worker takes over
    report = w.worker("w2").run_once()
    assert report is not None and report.state is JobState.DONE
    job = w.store.get("job-1")
    assert job.plan == saved.plan and job.step_index == 5
    # The new worker made exactly one model call (step 5) and no reads again.
    assert w.models[1].calls == 1 and w.go.reads == reads_before
    tools = [a["tool"] for a in w.go.applied]
    assert tools == ["update_plan", "send_reminder", "notify_rep", "send_to_user"]
    assert w.go.applied[: len(applied_before)] == applied_before
    keys = [a["idempotency_key"] for a in w.go.applied]
    assert len(keys) == len(set(keys))
    # Step 5 saw the plan saved in step 1, from the job, not from memory.
    first = w.models[1].seen[0]
    assert "chasing shop-005" in first[1]["content"] or any(
        "update_plan" in json.dumps(m) for m in first)


def test_after_a_turn_ends_no_worker_holds_state() -> None:
    w = World()
    w.job(goal="shop-005 says it cannot pay")
    ask = [say("", ("get_balance", {"shop_id": "shop-005"})),
           say("", ("ask_question", {"to": "owner", "question": "shop-005 says it cannot "
                                     "pay. Hand it to the rep?"})),
           say("", ("notify_rep", {"shop_id": "shop-005", "message": "Owner says visit"}),
               ("send_to_user", {"channel": "owner_console", "body": "Rep told.",
                                 "ends_turn": True}))]
    r = w.worker("w1", ask).run_once()
    assert r is not None and r.state is JobState.WAITING_ON_OWNER
    # Every worker is gone. The owner answers; a brand-new worker finishes.
    w.store.post_owner_message("job-1", OwnerMessage("m1", "yes, send the rep", T0))
    r2 = w.worker("w-new", ask).run_once()
    assert r2 is not None and r2.state is JobState.DONE
    seen = w.models[-1].seen[0]
    assert any("yes, send the rep" in str(m.get("content")) for m in seen)
    assert any(m.get("role") == "tool" and "owes" in m["content"] or "outstanding" in
               m.get("content", "") for m in seen)  # the first turn's read, from the store
    assert w.models[-1].calls == 1


def test_daily_naira_cap_parks_jobs_and_asks_the_owner_once() -> None:
    w = World(daily_cap_kobo=1)
    w.go.add_spend(TENANT, "collector", T0.date(), 5)
    for jid in ("job-a", "job-b", "job-c"):
        w.job(jid)
    for i in range(3):
        rep = w.worker(f"w{i}").run_once()
        assert rep is not None and rep.state is JobState.WAITING_ON_OWNER
    questions = [a for a in w.go.applied if a["payload"].get("kind") == "question"
                 and "allowance" in a["payload"].get("body", "")]
    assert len(questions) == 1
    assert sum(m.calls for m in w.models) == 0
    for jid in ("job-a", "job-b", "job-c"):
        assert "allowance" in (w.store.get(jid).waiting_reason or "")


def test_step_spans_carry_tool_short_result_duration_and_naira() -> None:
    w = World()
    w.job()
    w.worker("w1").run_once()
    spans = [s for s in w.tracer.spans if s.attrs.get("harness")]
    assert len(spans) == 5
    for s in spans:
        for key in ("tool", "short", "duration_ms", "cost_kobo", "wake_source"):
            assert key in s.attrs, (s.name, key)
    assert spans[1].attrs["tool"] == "get_balance" and "owes" in str(spans[1].attrs["short"])


def test_kill_switch_blocks_the_job_at_the_next_step() -> None:
    w = World()
    w.job()
    w.go.killed_helpers.add((TENANT, "collector"))
    rep = w.worker("w1").run_once()
    assert rep is not None and rep.state is JobState.BLOCKED
    assert w.models[0].calls == 0


def test_resumed_job_replays_its_last_intent_with_the_same_key_and_go_sends_nothing_new() -> None:
    w = World()
    w.job()

    class DyingSink:
        """Go accepts the reminder, then the worker dies before recording it."""

        def __init__(self) -> None:
            self.died = False

        def submit(self, tenant: str, key: str, kind: str, payload: dict[str, Any]) -> str:
            out = w.go.submit(tenant, key, kind, payload)
            if payload["intent_kind"] == "send_reminder" and not self.died:
                self.died = True
                raise Crash()
            return out

    runner = w.worker("w1")
    runner.sink = DyingSink()
    with pytest.raises(Crash):
        runner.run_once()
    sends_before = len([a for a in w.go.applied if a["tool"] == "send_reminder"])
    w.now += timedelta(seconds=31)
    w.worker("w2").run_once()
    reminders = [a for a in w.go.applied if a["tool"] == "send_reminder"]
    assert sends_before == 1 and len(reminders) == 1
    assert w.go.submits > len(w.go.accepted)  # the resend happened and was a no-op


def test_replayed_retries_have_no_duplicate_side_effects() -> None:
    r = replay_suite(WORLD, build(WORLD), n=24)
    assert r["duplicate_side_effects"] == 0 and r["lost_intents"] == 0


def test_idle_distributor_makes_no_model_calls_apart_from_routines() -> None:
    r = idle_suite(WORLD)
    assert r["model_calls_outside_routines"] == 0 and r["model_calls"] == 1
    assert r["worker_polls"] == 24 * 60


def test_security_suite_refuses_every_case() -> None:
    r = security_suite(WORLD)
    assert r["cases"] >= 40 and r["refused"] == r["cases"], r["failures"]
