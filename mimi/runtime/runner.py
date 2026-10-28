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
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mimi.runtime.budgets import first_hit
from mimi.runtime.delegation import root_of, tree_spend
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
from mimi.tz import LAGOS

log = logging.getLogger(__name__)

STOP_WORDS = frozenset({"stop", "wait", "hold on", "pause", "cancel"})
CONTINUE_WORDS = frozenset({"yes", "continue", "keep going", "go on", "ok", "okay"})
MAX_CONSECUTIVE_FAILURES = 3
# Loop safety (docs/LOOP-SAFETY.md). A job that failed this many steps over
# its whole life, or whose worker died mid-step this many times, is a poison
# job: parked BLOCKED for a person, never retried forever.
MAX_TOTAL_FAILURES = 6
MAX_TAKEOVERS = 3
# Retries after a failed step wait 0.2 s, then 0.4 s (doubling, capped).
BACKOFF_BASE_S = 0.2
BACKOFF_MAX_S = 5.0
# A job may wake itself (sleep) this many times per Lagos day (hourly checks
# and burst waits fit), and never sooner than this after the step that asked.
MAX_SELF_WAKES_PER_DAY = 48
MIN_SLEEP_S = 30.0


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
        sleep: Callable[[float], None] = time.sleep,
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
        self.sleep = sleep

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

        poison = self._poison(job)
        if poison:
            job = self._park(job, JobState.BLOCKED, poison, ())
            return RunReport(job.job_id, job.state, 0)

        granted = False
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

            said_continue = any(_norm(t) in CONTINUE_WORDS for t in texts)
            hit = first_hit(job.budget, job.spend)
            if hit is not None and said_continue:
                job.budget = job.budget.extended(job.budget_grant)
                hit = first_hit(job.budget, job.spend)
            if hit is None and job.root_job_id and job.root_job_id != job.job_id:
                # A child job runs inside its root's budget (mimi.runtime.delegation).
                root = self.store.get(root_of(job))
                hit = first_hit(root.budget, tree_spend(self.store, root_of(job)))
            granted = granted or said_continue
            if said_continue:
                job.self_wakes = {}  # the owner said keep going: the day's count starts over
            if hit is not None:
                job.owner_messages.extend(texts)
                job = self._park(
                    job, JobState.WAITING_ON_OWNER, f"budget: {hit.question()}", ack
                )
                break

            job = self._step(job, step_fn, texts, ack, granted=granted)
            granted = False
            steps_run += 1
            if job.state is JobState.WORKING and job.failures and job.after_flush is None:
                # Back off before retrying a failed step; a poison job parks.
                poison = self._poison(job)
                if poison:
                    job = self._park(job, JobState.BLOCKED, poison, ())
                    break
                self.sleep(min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** (job.failures - 1)))

        return RunReport(job.job_id, job.state, steps_run)

    @staticmethod
    def _poison(job: Job) -> str:
        if job.total_failures >= MAX_TOTAL_FAILURES:
            return (f"poison: {job.total_failures} failed steps over this job's life; "
                    "parked for a person, not retried")
        if job.takeovers >= MAX_TAKEOVERS:
            return (f"poison: the worker stopped mid-step {job.takeovers} times; parked "
                    "for a person, not retried")
        return ""

    @staticmethod
    def _owner_says_stop(inbox: list[OwnerMessage]) -> bool:
        return any(_norm(m.text) in STOP_WORDS for m in inbox)

    def _step(
        self, job: Job, step_fn: StepFn, texts: tuple[str, ...], ack: tuple[str, ...],
        *, granted: bool = False,
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
            thread_id=job.thread_id,
            wake_source=job.wake_source,
            scratch=job.copy().scratch,
            lineage=tuple(job.chain[:-1]),
            granted=granted,
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
        if outcome.scratch is not None:
            job.scratch = dict(outcome.scratch)
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
            now = self.clock()
            day = now.astimezone(LAGOS).date().isoformat()
            wakes = job.self_wakes.get(day, 0) + 1
            if not raw:
                job.move(JobState.BLOCKED)
                job.waiting_reason = "sleep without a wake time"
            elif wakes > MAX_SELF_WAKES_PER_DAY:
                # A routine that keeps re-scheduling itself is capped per day.
                job.move(JobState.WAITING_ON_OWNER)
                job.waiting_reason = (f"this job woke itself {wakes - 1} times today "
                                      f"(the limit is {MAX_SELF_WAKES_PER_DAY}); paused "
                                      "until the owner says keep going")
            else:
                job.self_wakes = {day: wakes}
                job.move(JobState.SLEEPING_UNTIL)
                # Never an instant loop: a wake in the past or right now waits
                # at least 30 seconds.
                job.sleeping_until = max(datetime.fromisoformat(raw),
                                         now + timedelta(seconds=MIN_SLEEP_S))
        elif kind == "blocked":
            job.move(JobState.BLOCKED)
            job.waiting_reason = decision.get("question") or "blocked"
        return self._save(job)

    def _failed_step(self, job: Job, exc: Exception, started: datetime) -> Job:
        log.warning("step %s#%d failed: %r", job.job_id, job.step_index, exc)
        job.failures += 1
        job.total_failures += 1
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
