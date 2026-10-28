"""The durable job record and its JSON form.

Everything a worker needs to resume lives here, so a fresh process can pick a
job up from the store and carry on at ``step_index`` without any in-memory
state from the worker that crashed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from mimi.runtime.budgets import Budget, Spend
from mimi.runtime.states import JobState, check_transition


def _dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass(frozen=True)
class OwnerMessage:
    """An owner message waiting for the next step boundary."""

    message_id: str
    text: str
    received_at: datetime


@dataclass(frozen=True)
class PendingOutbound:
    """An intent the outbox still has to hand to Go.

    Written to the job before it is submitted, so a crash between the step and
    the submit resends the same key instead of losing or doubling the intent.
    """

    idempotency_key: str
    kind: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "kind": self.kind,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PendingOutbound:
        return cls(raw["idempotency_key"], raw["kind"], dict(raw["payload"]))


@dataclass
class Job:
    job_id: str
    tenant_id: str
    helper: str
    thread_id: str
    wake_source: str
    goal: str
    state: JobState = JobState.QUEUED
    step_index: int = 0
    plan: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)
    working_note: str = ""
    # Opaque, JSON-safe state a helper's step keeps between steps (the
    # harness keeps its transcript and step log here). Saved in the same
    # compare-and-set as the step, so it can never run ahead of the job.
    scratch: dict[str, Any] = field(default_factory=dict)
    budget: Budget = field(default_factory=Budget)
    # What one owner "yes, keep going" adds on top of ``budget``.
    budget_grant: Budget = field(default_factory=Budget)
    spend: Spend = field(default_factory=Spend)
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    sleeping_until: datetime | None = None
    waiting_reason: str | None = None
    # Owner messages already taken at a step boundary, newest last.
    owner_messages: list[str] = field(default_factory=list)
    pending_outbound: list[PendingOutbound] = field(default_factory=list)
    sent_keys: list[str] = field(default_factory=list)
    result: str | None = None
    # The step's park/finish decision, applied once the outbox is flushed, so
    # a job never parks with unsent intents.
    after_flush: dict[str, Any] | None = None
    # Consecutive failed steps; three in a row blocks the job.
    failures: int = 0
    # Loop safety (docs/LOOP-SAFETY.md). Failed steps over the job's whole
    # life (a poison job is parked, never retried forever); lease takeovers
    # (a worker died mid-step); self-wakes per Lagos day; the job tree:
    # its parent, its root and the helper chain from the root, this job last.
    total_failures: int = 0
    takeovers: int = 0
    self_wakes: dict[str, int] = field(default_factory=dict)
    parent_job_id: str = ""
    root_job_id: str = ""
    chain: list[str] = field(default_factory=list)
    # Optimistic concurrency: every save must name the version it read.
    version: int = 0

    def move(self, dst: JobState) -> None:
        check_transition(self.state, dst)
        self.state = dst

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "tenant_id": self.tenant_id,
            "helper": self.helper,
            "thread_id": self.thread_id,
            "wake_source": self.wake_source,
            "goal": self.goal,
            "state": self.state.value,
            "step_index": self.step_index,
            "plan": list(self.plan),
            "todo": list(self.todo),
            "working_note": self.working_note,
            "scratch": self.scratch,
            "budget": self.budget.to_dict(),
            "budget_grant": self.budget_grant.to_dict(),
            "spend": self.spend.to_dict(),
            "lease_owner": self.lease_owner,
            "lease_expires_at": _iso(self.lease_expires_at),
            "sleeping_until": _iso(self.sleeping_until),
            "waiting_reason": self.waiting_reason,
            "owner_messages": list(self.owner_messages),
            "pending_outbound": [p.to_dict() for p in self.pending_outbound],
            "sent_keys": list(self.sent_keys),
            "result": self.result,
            "after_flush": self.after_flush,
            "failures": self.failures,
            "total_failures": self.total_failures,
            "takeovers": self.takeovers,
            "self_wakes": dict(self.self_wakes),
            "parent_job_id": self.parent_job_id,
            "root_job_id": self.root_job_id,
            "chain": list(self.chain),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Job:
        return cls(
            job_id=raw["job_id"],
            tenant_id=raw["tenant_id"],
            helper=raw["helper"],
            thread_id=raw["thread_id"],
            wake_source=raw["wake_source"],
            goal=raw["goal"],
            state=JobState(raw["state"]),
            step_index=int(raw["step_index"]),
            plan=list(raw["plan"]),
            todo=list(raw["todo"]),
            working_note=raw["working_note"],
            scratch=dict(raw.get("scratch") or {}),
            budget=Budget.from_dict(raw["budget"]),
            budget_grant=Budget.from_dict(raw["budget_grant"]),
            spend=Spend.from_dict(raw["spend"]),
            lease_owner=raw["lease_owner"],
            lease_expires_at=_dt(raw["lease_expires_at"]),
            sleeping_until=_dt(raw["sleeping_until"]),
            waiting_reason=raw["waiting_reason"],
            owner_messages=list(raw["owner_messages"]),
            pending_outbound=[
                PendingOutbound.from_dict(p) for p in raw["pending_outbound"]
            ],
            sent_keys=list(raw["sent_keys"]),
            result=raw["result"],
            after_flush=raw.get("after_flush"),
            failures=int(raw["failures"]),
            total_failures=int(raw.get("total_failures") or 0),
            takeovers=int(raw.get("takeovers") or 0),
            self_wakes={str(k): int(v) for k, v in (raw.get("self_wakes") or {}).items()},
            parent_job_id=str(raw.get("parent_job_id") or ""),
            root_job_id=str(raw.get("root_job_id") or ""),
            chain=list(raw.get("chain") or []),
            version=int(raw["version"]),
        )

    def copy(self) -> Job:
        """A detached copy: what a store hands out is never shared state."""
        return Job.from_dict(self.to_dict())
