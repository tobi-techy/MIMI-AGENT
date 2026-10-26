"""Job store contract, plus the in-memory store tests and the demo use.

The store is the only state. Three rules make crash recovery safe:

* ``claim`` hands a job to one worker under a lease. A job whose lease has
  expired is claimable again, which is how a crashed worker's job is taken over.
* ``save`` is compare-and-set on ``(version, lease_owner)``. A worker that lost
  its lease (it stalled, another worker took over) gets ``LeaseLost`` and must
  stop: it can never overwrite the newer worker's progress.
* Owner messages sit in their own inbox and are acknowledged in the same save
  that records them on the job, so a crash never loses or doubles one.

The Postgres store is ``mimi.runtime.sql_store.SqlJobStore``.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Protocol

from mimi.runtime.job import Job, OwnerMessage
from mimi.runtime.states import JobState


class LeaseLost(RuntimeError):
    """This worker no longer holds the job. Stop without writing."""


class JobNotFound(KeyError):
    pass


def is_claimable(job: Job, now: datetime) -> bool:
    if job.state is JobState.QUEUED:
        return True
    if job.state is JobState.SLEEPING_UNTIL:
        return job.sleeping_until is not None and job.sleeping_until <= now
    if job.state is JobState.WORKING:
        # Takeover: the holder stopped heartbeating.
        return job.lease_expires_at is None or job.lease_expires_at <= now
    return False


def wakes_on_owner_message(job: Job) -> bool:
    return job.state in (JobState.WAITING_ON_OWNER, JobState.SLEEPING_UNTIL)


class JobStore(Protocol):
    def create(self, job: Job) -> None: ...

    def get(self, job_id: str) -> Job: ...

    def claim(self, worker_id: str, now: datetime, lease_s: float) -> Job | None: ...

    def heartbeat(
        self, job_id: str, worker_id: str, now: datetime, lease_s: float
    ) -> datetime: ...

    def save(
        self, job: Job, worker_id: str, ack_message_ids: tuple[str, ...] = ()
    ) -> Job: ...

    def post_owner_message(self, job_id: str, message: OwnerMessage) -> None: ...

    def pending_owner_messages(self, job_id: str) -> list[OwnerMessage]: ...

    def wake(self, job_id: str) -> bool: ...


class InMemoryJobStore:
    """Thread-safe and per process. Same semantics as the SQL store."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._inbox: dict[str, list[OwnerMessage]] = {}
        self._acked: set[str] = set()
        self._lock = threading.Lock()

    def create(self, job: Job) -> None:
        with self._lock:
            if job.job_id in self._jobs:
                raise ValueError(f"job {job.job_id} exists")
            self._jobs[job.job_id] = job.copy()
            self._inbox.setdefault(job.job_id, [])

    def get(self, job_id: str) -> Job:
        with self._lock:
            return self._get(job_id).copy()

    def _get(self, job_id: str) -> Job:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise JobNotFound(job_id) from exc

    def claim(self, worker_id: str, now: datetime, lease_s: float) -> Job | None:
        with self._lock:
            # Oldest step first is not a priority scheme; jobs with owner
            # messages waiting go first (owner > shop > routine).
            candidates = [j for j in self._jobs.values() if is_claimable(j, now)]
            if not candidates:
                return None
            candidates.sort(
                key=lambda j: (not self._has_unacked(j.job_id), j.job_id),
            )
            job = candidates[0]
            job.move(JobState.WORKING)
            job.lease_owner = worker_id
            job.lease_expires_at = now + timedelta(seconds=lease_s)
            job.sleeping_until = None
            job.waiting_reason = None
            job.version += 1
            return job.copy()

    def heartbeat(
        self, job_id: str, worker_id: str, now: datetime, lease_s: float
    ) -> datetime:
        with self._lock:
            job = self._get(job_id)
            if job.lease_owner != worker_id or job.state is not JobState.WORKING:
                raise LeaseLost(job_id)
            job.lease_expires_at = now + timedelta(seconds=lease_s)
            return job.lease_expires_at

    def save(
        self, job: Job, worker_id: str, ack_message_ids: tuple[str, ...] = ()
    ) -> Job:
        with self._lock:
            current = self._get(job.job_id)
            if current.version != job.version or current.lease_owner != worker_id:
                raise LeaseLost(job.job_id)
            stored = job.copy()
            stored.version += 1
            if stored.state is not JobState.WORKING:
                stored.lease_owner = None
                stored.lease_expires_at = None
            else:
                # The heartbeat may have moved the lease since the worker read it.
                stored.lease_expires_at = current.lease_expires_at
            self._jobs[job.job_id] = stored
            self._acked.update(ack_message_ids)
            return stored.copy()

    def post_owner_message(self, job_id: str, message: OwnerMessage) -> None:
        with self._lock:
            job = self._get(job_id)
            inbox = self._inbox.setdefault(job_id, [])
            if any(m.message_id == message.message_id for m in inbox):
                return  # WhatsApp retried; same message, same effect.
            inbox.append(message)
            if wakes_on_owner_message(job):
                job.move(JobState.QUEUED)
                job.sleeping_until = None
                job.version += 1

    def wake(self, job_id: str) -> bool:
        """An outside event (a shop reply) wakes a sleeping job now.

        Only ``sleeping_until`` wakes: a parked owner question or a blocked
        job waits for a person, and a working job sees the event at its next
        step. Returns whether the job was woken.
        """
        with self._lock:
            job = self._get(job_id)
            if job.state is not JobState.SLEEPING_UNTIL:
                return False
            job.move(JobState.QUEUED)
            job.sleeping_until = None
            job.version += 1
            return True

    def pending_owner_messages(self, job_id: str) -> list[OwnerMessage]:
        with self._lock:
            return [
                m
                for m in self._inbox.get(job_id, [])
                if m.message_id not in self._acked
            ]

    def _has_unacked(self, job_id: str) -> bool:
        return any(m.message_id not in self._acked for m in self._inbox[job_id])
