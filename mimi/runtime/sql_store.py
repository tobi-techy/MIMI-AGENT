"""SQL job store (Postgres in production, SQLite in tests).

Same contract as ``InMemoryJobStore``. Every write is a compare-and-set on
``version`` in a single UPDATE, so two workers racing for one job cannot both
win, on any dialect, without relying on row locks. The job body is JSON; the
columns beside it exist for the claim query and the console.

Tables are created by alembic revision 0001 (``runtime_jobs`` and
``runtime_owner_inbox``); ``ensure_schema`` is for tests and the local demo.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.runtime.job import Job, OwnerMessage
from mimi.runtime.states import JobState
from mimi.runtime.store import (
    JobNotFound,
    LeaseLost,
    is_claimable,
    wakes_on_owner_message,
)

metadata = sa.MetaData()

jobs = sa.Table(
    "runtime_jobs",
    metadata,
    sa.Column("job_id", sa.String(), primary_key=True),
    sa.Column("tenant_id", sa.String(), nullable=False, index=True),
    sa.Column("helper", sa.String(), nullable=False),
    sa.Column("state", sa.String(), nullable=False, index=True),
    sa.Column("lease_owner", sa.String(), nullable=True),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("body", sa.Text(), nullable=False),
)

inbox = sa.Table(
    "runtime_owner_inbox",
    metadata,
    sa.Column("message_id", sa.String(), primary_key=True),
    sa.Column("job_id", sa.String(), nullable=False, index=True),
    sa.Column("text", sa.Text(), nullable=False),
    sa.Column("received_at", sa.String(), nullable=False),
    sa.Column("acked", sa.Boolean(), nullable=False, default=False),
)

_CLAIM_STATES = (
    JobState.QUEUED.value,
    JobState.SLEEPING_UNTIL.value,
    JobState.WORKING.value,
)


def _row(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "tenant_id": job.tenant_id,
        "helper": job.helper,
        "state": job.state.value,
        "lease_owner": job.lease_owner,
        "version": job.version,
        "body": json.dumps(job.to_dict(), sort_keys=True),
    }


class SqlJobStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def ensure_schema(self) -> None:
        metadata.create_all(self.engine)

    # ---------------------------------------------------------------- reads

    def create(self, job: Job) -> None:
        with self.engine.begin() as conn:
            conn.execute(jobs.insert().values(**_row(job)))

    def get(self, job_id: str) -> Job:
        with self.engine.connect() as conn:
            return self._load(conn, job_id)

    @staticmethod
    def _load(conn: sa.Connection, job_id: str) -> Job:
        body = conn.execute(
            sa.select(jobs.c.body).where(jobs.c.job_id == job_id)
        ).scalar_one_or_none()
        if body is None:
            raise JobNotFound(job_id)
        return Job.from_dict(json.loads(body))

    # ---------------------------------------------------------------- writes

    @staticmethod
    def _cas(conn: sa.Connection, job: Job, expected_version: int) -> bool:
        result = conn.execute(
            jobs.update()
            .where(jobs.c.job_id == job.job_id, jobs.c.version == expected_version)
            .values(**_row(job))
        )
        return result.rowcount == 1

    def tree(self, root_job_id: str) -> list[Job]:
        """Every job of one job tree: the root and every job under it."""
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(jobs.c.body)).scalars()
            out = [Job.from_dict(json.loads(b)) for b in rows]
        return [j for j in out if j.job_id == root_job_id or j.root_job_id == root_job_id]

    def claim(self, worker_id: str, now: datetime, lease_s: float) -> Job | None:
        with self.engine.begin() as conn:
            rows = conn.execute(
                sa.select(jobs.c.body)
                .where(jobs.c.state.in_(_CLAIM_STATES))
                .order_by(jobs.c.job_id)
            ).scalars()
            candidates = [Job.from_dict(json.loads(b)) for b in rows]
            waiting = set(
                conn.execute(
                    sa.select(inbox.c.job_id).where(inbox.c.acked.is_(False))
                ).scalars()
            )
        candidates = [j for j in candidates if is_claimable(j, now)]
        candidates.sort(key=lambda j: (j.job_id not in waiting, j.job_id))
        for job in candidates:
            expected = job.version
            if job.state is JobState.WORKING:
                # A lapsed lease: the last holder died mid-step.
                job.takeovers += 1
            job.move(JobState.WORKING)
            job.lease_owner = worker_id
            job.lease_expires_at = now + timedelta(seconds=lease_s)
            job.sleeping_until = None
            job.waiting_reason = None
            job.version = expected + 1
            with self.engine.begin() as conn:
                if self._cas(conn, job, expected):
                    return job
        return None

    def heartbeat(
        self, job_id: str, worker_id: str, now: datetime, lease_s: float
    ) -> datetime:
        with self.engine.begin() as conn:
            job = self._load(conn, job_id)
            if job.lease_owner != worker_id or job.state is not JobState.WORKING:
                raise LeaseLost(job_id)
            # The heartbeat does not bump the version: a worker's in-hand copy
            # stays valid, and the lease time lives only in the stored body.
            job.lease_expires_at = now + timedelta(seconds=lease_s)
            if not self._cas(conn, job, job.version):
                raise LeaseLost(job_id)
            return job.lease_expires_at

    def save(
        self, job: Job, worker_id: str, ack_message_ids: tuple[str, ...] = ()
    ) -> Job:
        with self.engine.begin() as conn:
            current = self._load(conn, job.job_id)
            if current.version != job.version or current.lease_owner != worker_id:
                raise LeaseLost(job.job_id)
            stored = job.copy()
            stored.version = job.version + 1
            if stored.state is not JobState.WORKING:
                stored.lease_owner = None
                stored.lease_expires_at = None
            else:
                stored.lease_expires_at = current.lease_expires_at
            if not self._cas(conn, stored, job.version):
                raise LeaseLost(job.job_id)
            if ack_message_ids:
                conn.execute(
                    inbox.update()
                    .where(inbox.c.message_id.in_(ack_message_ids))
                    .values(acked=True)
                )
            return stored

    def post_owner_message(self, job_id: str, message: OwnerMessage) -> None:
        with self.engine.begin() as conn:
            seen = conn.execute(
                sa.select(inbox.c.message_id).where(
                    inbox.c.message_id == message.message_id
                )
            ).first()
            if seen is not None:
                return
            conn.execute(
                inbox.insert().values(
                    message_id=message.message_id,
                    job_id=job_id,
                    text=message.text,
                    received_at=message.received_at.isoformat(),
                    acked=False,
                )
            )
            job = self._load(conn, job_id)
            if wakes_on_owner_message(job):
                expected = job.version
                job.move(JobState.QUEUED)
                job.sleeping_until = None
                job.version = expected + 1
                if not self._cas(conn, job, expected):
                    raise LeaseLost(job_id)

    def wake(self, job_id: str) -> bool:
        """Same contract as ``InMemoryJobStore.wake``."""
        with self.engine.begin() as conn:
            job = self._load(conn, job_id)
            if job.state is not JobState.SLEEPING_UNTIL:
                return False
            expected = job.version
            job.move(JobState.QUEUED)
            job.sleeping_until = None
            job.version = expected + 1
            return self._cas(conn, job, expected)

    def pending_owner_messages(self, job_id: str) -> list[OwnerMessage]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                sa.select(inbox.c.message_id, inbox.c.text, inbox.c.received_at)
                .where(inbox.c.job_id == job_id, inbox.c.acked.is_(False))
                .order_by(inbox.c.received_at, inbox.c.message_id)
            ).all()
        return [
            OwnerMessage(r.message_id, r.text, datetime.fromisoformat(r.received_at))
            for r in rows
        ]
