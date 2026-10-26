"""The worker: claim a job, run it step by step, save after every step.

One pass of the loop, in order:

1. flush the outbox (intents a crashed worker recorded but may not have sent);
2. heartbeat the lease (lost it? stop without writing);
3. take owner messages at the boundary ("stop" parks the job at once);
4. check the budgets (hit one: park and ask the owner);
5. run one step inside a span;
6. record its intents in the outbox with idempotency keys;
7. apply the outcome and save (compare-and-set), then flush.

A crash at any point loses at most the step in flight. A step that finished
and was saved is never run again; an intent is submitted at least once and
accepted exactly once because its key is stable.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from mimi.runtime.budgets import first_hit
from mimi.runtime.job import Job, OwnerMessage, PendingOutbound
from mimi.runtime.outbox import (
    ALLOWED_INTENT_TYPES,
    ForbiddenIntent,
    IntentSink,
    idempotency_key,
    intent_payload,
)
from mimi.runtime.states import JobState
from mimi.runtime.steps import StepContext, StepFn, StepOutcome
from mimi.runtime.store import JobStore, LeaseLost
from mimi.runtime.tracing import StepTracer

log = logging.getLogger(__name__)

STOP_WORDS = frozenset({"stop", "wait", "hold on", "pause", "cancel"})
CONTINUE_WORDS = frozenset({"yes", "continue", "keep going", "go on", "ok", "okay"})
MAX_CONSECUTIVE_FAILURES = 3


def _norm(text: str) -> str:
    return " ".join(text.lower().strip(" .!").split())


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RunReport:
    job_id: str
    state: JobState
    steps_run: int
    lease_lost: bool = False


class JobRunner:
    def __init__(
        self,
        *,
        store: JobStore,
        sink: IntentSink,
        tracer: StepTracer,
        steps: dict[str, StepFn],
        worker_id: str,
        lease_s: float = 30.0,
        clock: Callable[[], datetime] = _utcnow,
        intent_types: tuple[type, ...] = ALLOWED_INTENT_TYPES,
    ) -> None:
        self.store = store
        self.sink = sink
        self.tracer = tracer
        self.steps = steps
        self.worker_id = worker_id
        self.lease_s = lease_s
        self.clock = clock
        # Build the list with ``outbox.intent_types``, which refuses money
        # verbs; the default is empty, so a runner accepts nothing undeclared.
        self.intent_types = intent_types

    # ------------------------------------------------------------------ API

    def run_once(self) -> RunReport | None:
        """Claim one runnable job and drive it until it parks or finishes."""
        job = self.store.claim(self.worker_id, self.clock(), self.lease_s)
        if job is None:
            return None
        try:
            return self._drive(job)
        except LeaseLost:
            log.warning("lease lost on %s; another worker owns it", job.job_id)
            return RunReport(job.job_id, JobState.WORKING, 0, lease_lost=True)

    def run_forever(self, stop: threading.Event, idle_s: float = 1.0) -> None:
        while not stop.is_set():
            if self.run_once() is None:
                stop.wait(idle_s)

    # ------------------------------------------------------------- internals

    def _save(self, job: Job, ack: tuple[str, ...] = ()) -> Job:
        return self.store.save(job, self.worker_id, ack)

    def _flush(self, job: Job) -> Job:
        if not job.pending_outbound:
            return job
        for item in list(job.pending_outbound):
            if item.idempotency_key not in job.sent_keys:
                self.sink.submit(
                    job.tenant_id, item.idempotency_key, item.kind, item.payload
                )
                job.sent_keys.append(item.idempotency_key)
        job.pending_outbound = []
        return self._save(job)

    def _park(
        self, job: Job, state: JobState, reason: str, ack: tuple[str, ...]
    ) -> Job:
        job.move(state)
        job.waiting_reason = reason
        return self._save(job, ack)

    def _drive(self, job: Job) -> RunReport:
        steps_run = 0
        step_fn = self.steps.get(job.helper)
        if step_fn is None:
            job = self._park(job, JobState.BLOCKED, f"no helper {job.helper!r}", ())
            return RunReport(job.job_id, job.state, 0)

        while job.state is JobState.WORKING:
            try:
                job = self._flush(job)
            except LeaseLost:
                raise
            except Exception:
                log.exception("outbox flush failed for %s; requeue", job.job_id)
                job.move(JobState.QUEUED)
                job = self._save(job)
                break
            if job.after_flush is not None:
                job = self._settle(job)
                continue

            self.store.heartbeat(job.job_id, self.worker_id, self.clock(), self.lease_s)

            inbox = self.store.pending_owner_messages(job.job_id)
            ack = tuple(m.message_id for m in inbox)
            texts = tuple(m.text for m in inbox)
            if self._owner_says_stop(inbox):
                job.owner_messages.extend(texts)
                job = self._park(job, JobState.WAITING_ON_OWNER, "owner said stop", ack)
                break

            hit = first_hit(job.budget, job.spend)
            if hit is not None and any(_norm(t) in CONTINUE_WORDS for t in texts):
                job.budget = job.budget.extended(job.budget_grant)
                hit = first_hit(job.budget, job.spend)
            if hit is not None:
                job.owner_messages.extend(texts)
                job = self._park(
                    job, JobState.WAITING_ON_OWNER, f"budget: {hit.question()}", ack
                )
                break

            job = self._step(job, step_fn, texts, ack)
            steps_run += 1

        return RunReport(job.job_id, job.state, steps_run)

    @staticmethod
    def _owner_says_stop(inbox: list[OwnerMessage]) -> bool:
        return any(_norm(m.text) in STOP_WORDS for m in inbox)

    def _step(
        self, job: Job, step_fn: StepFn, texts: tuple[str, ...], ack: tuple[str, ...]
    ) -> Job:
        started = self.clock()
        ctx = StepContext(
            job_id=job.job_id,
            tenant_id=job.tenant_id,
            helper=job.helper,
            goal=job.goal,
            step_index=job.step_index,
            plan=tuple(job.plan),
            todo=tuple(job.todo),
            working_note=job.working_note,
            new_owner_messages=texts,
            now=started,
        )
        with self.tracer.step(
            job_id=job.job_id,
            thread_id=job.thread_id,
            helper=job.helper,
            step_index=job.step_index,
        ) as span:
            span.set("wake_source", job.wake_source)
            span.set("owner_messages", len(texts))
            try:
                outcome = step_fn(ctx)
            except Exception as exc:
                span.set("error", type(exc).__name__)
                return self._failed_step(job, exc, started)
            try:
                pending = self._to_outbound(job, outcome, self.intent_types)
            except ForbiddenIntent as exc:
                span.set("error", "forbidden_intent")
                job.owner_messages.extend(texts)
                return self._park(job, JobState.BLOCKED, str(exc), ack)
            span.set("outcome", outcome.kind)
            span.set("intents", len(pending))
            span.set("cost_kobo", outcome.cost_kobo)
            for key, value in outcome.trace.items():
                span.set(key, value)

        job.failures = 0
        job.spend.steps += 1
        job.spend.seconds += (self.clock() - started).total_seconds()
        job.spend.kobo += max(0, outcome.cost_kobo)
        job.step_index += 1
        job.owner_messages.extend(texts)
        job.pending_outbound.extend(pending)
        if outcome.plan is not None:
            job.plan = list(outcome.plan)
        if outcome.todo is not None:
            job.todo = list(outcome.todo)
        if outcome.working_note is not None:
            job.working_note = outcome.working_note
        if outcome.kind != "continue":
            job.after_flush = {
                "kind": outcome.kind,
                "question": outcome.question,
                "result": outcome.result,
                "sleep_until": (
                    outcome.sleep_until.isoformat() if outcome.sleep_until else None
                ),
            }
        # Persisted after every step: the step's work, its outbox and its
        # decision land together, before anything leaves the process.
        return self._save(job, ack)

    def _settle(self, job: Job) -> Job:
        decision = job.after_flush or {}
        job.after_flush = None
        kind = decision.get("kind")
        if kind == "done":
            job.move(JobState.DONE)
            job.result = decision.get("result")
        elif kind == "ask_owner":
            job.move(JobState.WAITING_ON_OWNER)
            job.waiting_reason = decision.get("question") or "needs the owner"
        elif kind == "sleep":
            raw = decision.get("sleep_until")
            if not raw:
                job.move(JobState.BLOCKED)
                job.waiting_reason = "sleep without a wake time"
            else:
                job.move(JobState.SLEEPING_UNTIL)
                job.sleeping_until = datetime.fromisoformat(raw)
        elif kind == "blocked":
            job.move(JobState.BLOCKED)
            job.waiting_reason = decision.get("question") or "blocked"
        return self._save(job)

    def _failed_step(self, job: Job, exc: Exception, started: datetime) -> Job:
        log.warning("step %s#%d failed: %r", job.job_id, job.step_index, exc)
        job.failures += 1
        job.spend.seconds += (self.clock() - started).total_seconds()
        if job.failures >= MAX_CONSECUTIVE_FAILURES:
            job.move(JobState.BLOCKED)
            job.waiting_reason = f"step failed {job.failures} times: {exc!r}"
        # Owner messages stay unacknowledged: the retry sees them again.
        return self._save(job)

    @staticmethod
    def _to_outbound(
        job: Job, outcome: StepOutcome, allowed: tuple[type, ...]
    ) -> list[PendingOutbound]:
        out: list[PendingOutbound] = []
        for position, intent in enumerate(outcome.intents):
            kind, payload = intent_payload(intent, allowed)
            if payload.get("scope", {}).get("tenant_id") not in (None, job.tenant_id):
                raise ForbiddenIntent("intent scoped to another tenant")
            key = idempotency_key(job.job_id, job.step_index, position, kind, payload)
            out.append(PendingOutbound(key, kind, payload))
        return out
