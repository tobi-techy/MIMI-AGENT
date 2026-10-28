"""The durable turn queue: leases, compare-and-set, dedupe (RAI-266, RAI-272).

Each inbound message is a ``turns`` row, created in the same transaction as
its ``inbound_messages`` row; the unique key ``(tenant_id, external_id)``
makes a second delivery of the same message id a no-op that returns the
first turn.

Workers take turns with ``claim``:

* one turn per thread at a time: a thread with a live lease is skipped;
* the oldest waiting turn on a thread leads, and every other waiting turn on
  that thread joins its batch (state ``merged``), so a burst of messages is
  one turn that sees them all, as in the in-process front door;
* the claim is a compare-and-set on ``version`` and bumps the thread's
  ``claim_seq`` in the same transaction, so two workers can never both win a
  turn, or two turns of one thread, on SQLite or Postgres;
* a lease the holder stopped renewing expires and the turn is claimable
  again (takeover). The new holder carries on from the last checkpoint.

A worker saves the route decision and the harness state after every step
(``checkpoint``) and finishes with one compare-and-set that also writes the
turn's messages, intents and history (``mimi.api.agent``). A worker that lost
its lease fails that compare-and-set and writes nothing, so every turn
completes exactly once.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.harness.router import PRIORITY
from mimi.store import schema as s
from mimi.store.util import dumps, loads, now_iso

MAX_ATTEMPTS = 3


class _Raced(RuntimeError):
    """Another worker changed the turn between our read and our write."""


class LeaseLost(RuntimeError):
    """This worker no longer holds the turn. Stop without writing."""


def turn_id_for(tenant_id: str, external_id: str) -> str:
    """Stable per message: the same message id always names the same turn."""
    return "turn_" + hashlib.sha256(f"{tenant_id}\x00{external_id}".encode()).hexdigest()[:20]


@dataclass(frozen=True)
class Claim:
    turn_id: str
    tenant_id: str
    thread_id: str
    worker_id: str
    version: int
    attempts: int
    batch: tuple[str, ...]
    checkpoint: dict[str, Any]
    takeover: bool


class TurnQueue:
    def __init__(self, engine: Engine, *, lease_s: float = 30.0,
                 clock: Any = time.time) -> None:
        self.engine = engine
        self.lease_s = lease_s
        self.clock = clock

    # ------------------------------------------------------------- claim

    def claim(self, worker_id: str, turn_id: str | None = None) -> Claim | None:
        """Take the next runnable turn (or the named one, if it can run now)."""
        now = self.clock()
        with self.engine.connect() as conn:
            q = (sa.select(s.turns.c.turn_id, s.turns.c.tenant_id, s.turns.c.thread_id,
                           s.turns.c.state, s.turns.c.created_ts,
                           s.inbound_messages.c.source)
                 .select_from(s.turns.outerjoin(s.inbound_messages, sa.and_(
                     s.inbound_messages.c.tenant_id == s.turns.c.tenant_id,
                     s.inbound_messages.c.turn_id == s.turns.c.turn_id)))
                 .where(sa.or_(s.turns.c.state == "queued",
                               sa.and_(s.turns.c.state == "leased",
                                       s.turns.c.lease_expires_ts < now)))
                 .order_by(s.turns.c.created_ts, s.turns.c.turn_id))
            if turn_id is not None:
                row = conn.execute(sa.select(s.turns.c.tenant_id, s.turns.c.thread_id).where(
                    s.turns.c.turn_id == turn_id)).first()
                if row is None:
                    return None
                q = q.where(s.turns.c.tenant_id == row.tenant_id,
                            s.turns.c.thread_id == row.thread_id)
            candidates = conn.execute(q.limit(200)).all()
            conn.rollback()
        # A lapsed lease first (work already under way), then the owner, then
        # people, then systems and routines (RAI-272: the owner is handled
        # first); oldest first within each. A thread's oldest runnable turn
        # still leads that thread.
        by_thread: dict[tuple[str, str], Any] = {}
        for c in candidates:
            by_thread.setdefault((c.tenant_id, c.thread_id), c)
        candidates = sorted(by_thread.values(), key=lambda c: (
            0 if c.state == "leased" else 1, PRIORITY.get(c.source or "", 3),
            c.created_ts, c.turn_id))
        seen: set[tuple[str, str]] = set()
        for c in candidates:
            key = (c.tenant_id, c.thread_id)
            if key in seen:
                continue  # the oldest runnable turn on a thread leads
            seen.add(key)
            got = self._try_claim(worker_id, c.turn_id, now)
            if got is not None:
                return got
        return None

    def _try_claim(self, worker_id: str, turn_id: str, now: float) -> Claim | None:
        try:
            return self._claim_txn(worker_id, turn_id, now)
        except _Raced:
            return None

    def _claim_txn(self, worker_id: str, turn_id: str, now: float) -> Claim | None:
        with self.engine.begin() as conn:
            t = conn.execute(sa.select(s.turns).where(s.turns.c.turn_id == turn_id)).first()
            if t is None:
                return None
            takeover = t.state == "leased"
            if not (t.state == "queued" or (takeover and (t.lease_expires_ts or 0) < now)):
                return None
            # Serialise claims on this thread (row lock on Postgres, the write
            # lock on SQLite), then make sure nobody else holds it.
            conn.execute(s.threads.update().where(
                s.threads.c.tenant_id == t.tenant_id, s.threads.c.thread_id == t.thread_id)
                .values(claim_seq=s.threads.c.claim_seq + 1))
            busy = conn.execute(sa.select(s.turns.c.turn_id).where(
                s.turns.c.tenant_id == t.tenant_id, s.turns.c.thread_id == t.thread_id,
                s.turns.c.state == "leased", s.turns.c.lease_expires_ts >= now,
                s.turns.c.turn_id != turn_id)).first()
            if busy is not None:
                return None
            batch = list(loads(t.batch, []))
            if not takeover and t.solo:
                # A follow-up for one part of a message runs alone (RAI-301).
                batch = [r.external_id for r in conn.execute(
                    sa.select(s.inbound_messages.c.external_id).where(
                        s.inbound_messages.c.tenant_id == t.tenant_id,
                        s.inbound_messages.c.turn_id == turn_id))]
            elif not takeover:
                # Fresh claim: this turn leads every waiting turn on the thread.
                waiting = conn.execute(
                    sa.select(s.turns.c.turn_id).where(
                        s.turns.c.tenant_id == t.tenant_id,
                        s.turns.c.thread_id == t.thread_id,
                        s.turns.c.state == "queued", s.turns.c.turn_id != turn_id,
                        s.turns.c.solo.is_(False))
                    .order_by(s.turns.c.created_ts, s.turns.c.turn_id)).scalars().all()
                ids = [turn_id, *waiting]
                batch = [r.external_id for r in conn.execute(
                    sa.select(s.inbound_messages.c.external_id, s.inbound_messages.c.turn_id)
                    .where(s.inbound_messages.c.tenant_id == t.tenant_id,
                           s.inbound_messages.c.turn_id.in_(ids))
                    .order_by(s.inbound_messages.c.received_at,
                              s.inbound_messages.c.external_id))]
                if waiting:
                    conn.execute(s.turns.update().where(
                        s.turns.c.turn_id.in_(waiting), s.turns.c.state == "queued")
                        .values(state="merged", merged_into=turn_id,
                                version=s.turns.c.version + 1))
            res = conn.execute(s.turns.update().where(
                s.turns.c.turn_id == turn_id, s.turns.c.version == t.version)
                .values(state="leased", lease_owner=worker_id,
                        lease_expires_ts=now + self.lease_s, version=t.version + 1,
                        attempts=t.attempts + 1, batch=dumps(batch)))
            if res.rowcount != 1:
                raise _Raced(turn_id)  # rolls the merge back with it
            # Inbox rows for messages this claim now runs were never read by
            # the turn that was running: they are this turn's input now.
            conn.execute(s.thread_inbox.update().where(
                s.thread_inbox.c.tenant_id == t.tenant_id,
                s.thread_inbox.c.thread_id == t.thread_id,
                s.thread_inbox.c.status == "waiting",
                s.thread_inbox.c.external_id.in_(batch or [""]))
                .values(status="released", taken_by=turn_id, taken_at=now_iso(now)))
            conn.execute(s.turn_attempts.insert().values(
                turn_id=turn_id, worker_id=worker_id, takeover=takeover,
                claimed_at=now_iso(now)))
            return Claim(turn_id=turn_id, tenant_id=t.tenant_id, thread_id=t.thread_id,
                         worker_id=worker_id, version=t.version + 1,
                         attempts=t.attempts + 1, batch=tuple(batch),
                         checkpoint=dict(loads(t.checkpoint, {})), takeover=takeover)

    # ------------------------------------------------------------ leases

    def heartbeat(self, claim: Claim) -> bool:
        """Keep the lease alive. False means it was lost: stop."""
        with self.engine.begin() as conn:
            res = conn.execute(s.turns.update().where(
                s.turns.c.turn_id == claim.turn_id, s.turns.c.lease_owner == claim.worker_id,
                s.turns.c.state == "leased")
                .values(lease_expires_ts=self.clock() + self.lease_s))
            return bool(res.rowcount == 1)

    def checkpoint(self, claim: Claim, data: dict[str, Any]) -> Claim:
        """Save progress after a step (compare-and-set). Raises ``LeaseLost``."""
        with self.engine.begin() as conn:
            res = conn.execute(s.turns.update().where(
                s.turns.c.turn_id == claim.turn_id, s.turns.c.version == claim.version,
                s.turns.c.lease_owner == claim.worker_id, s.turns.c.state == "leased")
                .values(checkpoint=dumps(data), version=claim.version + 1))
            if res.rowcount != 1:
                raise LeaseLost(claim.turn_id)
        return Claim(**{**claim.__dict__, "version": claim.version + 1,
                        "checkpoint": dict(data)})

    def absorb(self, claim: Claim, data: dict[str, Any], *, merge: list[tuple[str, str]],
               inbox: list[tuple[str, str, dict[str, Any]]],
               extra: Any = None) -> Claim:
        """A running turn read its inbox at a step boundary (RAI-301). One
        compare-and-set saves the loop state with the absorbed messages in it,
        merges their turns into this one (their ids join the batch) and marks
        each inbox row ``absorbed``, ``released`` or ``cancelled_task``.

        ``merge`` is (turn_id, external_id) per absorbed message; ``inbox`` is
        (external_id, status, steer). A worker that lost its lease writes none
        of it, and the next holder reads the same rows again from the store.
        """
        now = self.clock()
        with self.engine.begin() as conn:
            batch = list(claim.batch)
            for turn_id, ext in merge:
                res = conn.execute(s.turns.update().where(
                    s.turns.c.turn_id == turn_id, s.turns.c.state == "queued",
                    s.turns.c.tenant_id == claim.tenant_id)
                    .values(state="merged", merged_into=claim.turn_id,
                            version=s.turns.c.version + 1))
                if res.rowcount == 1 and ext not in batch:
                    batch.append(ext)
            res = conn.execute(s.turns.update().where(
                s.turns.c.turn_id == claim.turn_id, s.turns.c.version == claim.version,
                s.turns.c.lease_owner == claim.worker_id, s.turns.c.state == "leased")
                .values(checkpoint=dumps(data), version=claim.version + 1,
                        batch=dumps(batch)))
            if res.rowcount != 1:
                raise LeaseLost(claim.turn_id)  # rolls the merges back with it
            for ext, status, steer in inbox:
                conn.execute(s.thread_inbox.update().where(
                    s.thread_inbox.c.tenant_id == claim.tenant_id,
                    s.thread_inbox.c.external_id == ext,
                    s.thread_inbox.c.status == "waiting")
                    .values(status=status, taken_by=claim.turn_id, steer=dumps(steer),
                            taken_at=now_iso(now)))
            if extra is not None:
                extra(conn)  # follow-up turns for released parts, same transaction
        return Claim(**{**claim.__dict__, "version": claim.version + 1,
                        "checkpoint": dict(data), "batch": tuple(batch)})

    def inbox_waiting(self, tenant_id: str, thread_id: str) -> list[Any]:
        """Messages waiting for this thread's running turn, owner first, then oldest."""
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(s.thread_inbox).where(
                s.thread_inbox.c.tenant_id == tenant_id,
                s.thread_inbox.c.thread_id == thread_id,
                s.thread_inbox.c.status == "waiting")).all()
        return sorted(rows, key=lambda r: (PRIORITY.get(r.source, 3), r.created_ts,
                                           r.external_id))

    def finish_in(self, conn: sa.Connection, claim: Claim, *, status: str, helper: str,
                  skill: str, run_id: str, result: dict[str, Any]) -> None:
        """The completing compare-and-set, inside the caller's transaction."""
        res = conn.execute(s.turns.update().where(
            s.turns.c.turn_id == claim.turn_id, s.turns.c.version == claim.version,
            s.turns.c.lease_owner == claim.worker_id, s.turns.c.state == "leased")
            .values(state="done", status=status, helper=helper, skill=skill, run_id=run_id,
                    result=dumps(result), lease_owner=None, lease_expires_ts=None,
                    version=claim.version + 1, completions=s.turns.c.completions + 1,
                    finished_at=now_iso()))
        if res.rowcount != 1:
            raise LeaseLost(claim.turn_id)

    def release(self, claim: Claim, error: str) -> str:
        """A step raised: back in the queue, or failed after ``MAX_ATTEMPTS``."""
        state = "failed" if claim.attempts >= MAX_ATTEMPTS else "queued"
        with self.engine.begin() as conn:
            conn.execute(s.turns.update().where(
                s.turns.c.turn_id == claim.turn_id, s.turns.c.version == claim.version,
                s.turns.c.lease_owner == claim.worker_id)
                .values(state=state, lease_owner=None, lease_expires_ts=None,
                        version=claim.version + 1, error=error[:2000],
                        status="failed" if state == "failed" else "",
                        finished_at=now_iso() if state == "failed" else None))
        return state

    # ------------------------------------------------------------- reads

    def get(self, turn_id: str) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(sa.select(s.turns).where(s.turns.c.turn_id == turn_id)).first()

    def depth(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(sa.select(sa.func.count()).select_from(s.turns).where(
                s.turns.c.state.in_(("queued", "leased")))).scalar() or 0)
