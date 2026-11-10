"""Retention: the policy table, the batched delete, and what must never go.

The rule under test: a consumed digest item and an old step row go; an
unconsumed digest item, a recent row, a six-year evidence row and every book
row stay. Runs on SQLite in CI and on Postgres when ``MIMI_TEST_DATABASE_URL``
is set (the delete uses ``rowid`` or ``ctid`` accordingly).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.store import ensure_schema, make_engine
from mimi.store import schema as s
from mimi.store.retention import (
    BY_TABLE,
    DISPUTE_EVIDENCE_DAYS,
    POLICIES,
    check_policies,
    plan,
    run,
)

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
ISO_OLD = (NOW - timedelta(days=10_000)).isoformat()
ISO_NEW = (NOW - timedelta(days=10)).isoformat()


@pytest.fixture()
def engine(store_url: str) -> Any:
    e = make_engine(store_url)
    ensure_schema(e)
    return e


def _ts(days: float) -> float:
    return (NOW - timedelta(days=days)).timestamp()


def _seed(engine: Any) -> None:
    with engine.begin() as conn:
        conn.execute(s.turns.insert(), [
            {"turn_id": "t-old-800", "tenant_id": "t", "thread_id": "th",
             "state": "done", "created_at": "2024-01-01T00:00:00+00:00",
             "created_ts": _ts(800)},
            {"turn_id": "t-new-10", "tenant_id": "t", "thread_id": "th",
             "state": "done", "created_at": "2026-10-31T00:00:00+00:00",
             "created_ts": _ts(10)},
        ])
        conn.execute(s.turn_attempts.insert(), [
            {"turn_id": "t-old-800", "worker_id": "w", "claimed_at": ISO_OLD,
             "takeover": False},
            {"turn_id": "t-new-10", "worker_id": "w", "claimed_at": ISO_NEW,
             "takeover": False},
        ])
        conn.execute(s.inbound_messages.insert(), [
            {"tenant_id": "t", "external_id": "old", "turn_id": "t-old-800",
             "thread_id": "th", "source": "shop_whatsapp", "envelope": "{}",
             "received_at": ISO_OLD},
            {"tenant_id": "t", "external_id": "new", "turn_id": "t-new-10",
             "thread_id": "th", "source": "shop_whatsapp", "envelope": "{}",
             "received_at": ISO_NEW},
        ])
        conn.execute(s.messages.insert(), [
            {"tenant_id": "t", "message_id": "m-old", "thread_id": "th",
             "direction": "in", "sender": "shop", "status": "sent", "at": ISO_OLD,
             "ts": _ts(2_200)},
            {"tenant_id": "t", "message_id": "m-evidence", "thread_id": "th",
             "direction": "in", "sender": "shop", "status": "sent", "at": ISO_NEW,
             "ts": _ts(2_000)},  # inside the six-year window: must stay
            {"tenant_id": "t", "message_id": "m-new", "thread_id": "th",
             "direction": "in", "sender": "shop", "status": "sent", "at": ISO_NEW,
             "ts": _ts(10)},
        ])
        conn.execute(s.thread_events.insert(), [
            {"tenant_id": "t", "event_id": "e-old", "thread_id": "th",
             "type": "step_finished", "at": ISO_OLD, "ts": _ts(800)},
            {"tenant_id": "t", "event_id": "e-new", "thread_id": "th",
             "type": "step_finished", "at": ISO_NEW, "ts": _ts(10)},
        ])
        conn.execute(s.history.insert(), [
            {"tenant_id": "t", "thread_id": "th", "turn_id": "t-old-800",
             "ts": _ts(800), "body": "[]"},
            {"tenant_id": "t", "thread_id": "th", "turn_id": "t-new-10",
             "ts": _ts(10), "body": "[]"},
        ])
        conn.execute(s.proof_events.insert(), [
            {"tenant_id": "t", "event_id": "pe-old", "record_type": "delivery",
             "record_id": "d1", "old_level": "estimated", "new_level": "likely",
             "witness_kind": "rep", "witness_source": "s", "policy_version": "v1",
             "at": ISO_OLD},
            {"tenant_id": "t", "event_id": "pe-new", "record_type": "delivery",
             "record_id": "d1", "old_level": "likely", "new_level": "verified",
             "witness_kind": "bank", "witness_source": "s", "policy_version": "v1",
             "at": ISO_NEW},
        ])
        conn.execute(s.digest_queue.insert(), [
            {"tenant_id": "t", "body": "old consumed", "kind": "reply",
             "day": "2024-01-01", "consumed": True, "created_at": ISO_OLD},
            {"tenant_id": "t", "body": "old unread", "kind": "reply",
             "day": "2024-01-01", "consumed": False, "created_at": ISO_OLD},
            {"tenant_id": "t", "body": "new consumed", "kind": "reply",
             "day": "2026-10-31", "consumed": True, "created_at": ISO_NEW},
        ])
        conn.execute(s.speak_counts.insert(), [
            {"tenant_id": "t", "audience": "shop", "shop_id": "s", "day": "2024-01-01",
             "created_at": ISO_OLD},
            {"tenant_id": "t", "audience": "shop", "shop_id": "s", "day": "2026-10-31",
             "created_at": ISO_NEW},
        ])
        conn.execute(s.routine_runs.insert(), [
            {"tenant_id": "t", "routine": "morning_brief", "day": "2024-01-01",
             "status": "done", "started_at": ISO_OLD, "started_ts": _ts(800)},
            {"tenant_id": "t", "routine": "morning_brief", "day": "2026-10-31",
             "status": "done", "started_at": ISO_NEW, "started_ts": _ts(10)},
        ])


def _count(engine: Any, table: str, where: str = "") -> int:
    t = s.metadata.tables[table]
    q = sa.select(sa.func.count()).select_from(t)
    if where:
        q = q.where(sa.text(where))
    with engine.connect() as conn:
        return int(conn.execute(q).scalar() or 0)


def test_the_policy_table_is_well_formed() -> None:
    assert check_policies() == []
    assert BY_TABLE["messages"].days >= DISPUTE_EVIDENCE_DAYS
    assert BY_TABLE["proof_events"].days >= DISPUTE_EVIDENCE_DAYS
    # The book is Go's: retention never names a money table.
    for table in ("deliveries", "receivables", "payments", "bank_feed", "approvals"):
        assert table not in BY_TABLE
    # Eight append-only tables the assignment names, plus the two counters.
    assert {"inbound_messages", "turns", "turn_attempts", "messages", "thread_events",
            "history", "proof_events", "digest_queue"} <= set(BY_TABLE)


def test_every_policy_has_a_reason_and_a_real_column() -> None:
    for p in POLICIES:
        assert len(p.reason) > 40, p.table
        assert p.table in s.metadata.tables
        assert p.column in s.metadata.tables[p.table].c


def test_plan_is_read_only_and_reports_cutoffs() -> None:
    rows = plan(NOW.timestamp())
    assert len(rows) == len(POLICIES)
    turns = next(r for r in rows if r["table"] == "turns")
    assert turns["days"] == 730
    assert float(turns["cutoff"]) < NOW.timestamp()
    inbound = next(r for r in rows if r["table"] == "inbound_messages")
    assert "2024" in str(inbound["cutoff"])


def test_dry_run_counts_and_deletes_nothing(engine: Any) -> None:
    _seed(engine)
    before = _count(engine, "turns")
    summary = run(engine, now=NOW.timestamp(), apply=False, sleep_s=0)
    by_table = {t["table"]: t for t in summary["tables"]}
    assert by_table["turns"]["rows"] == 1
    assert by_table["messages"]["rows"] == 1  # only the 2,200-day row
    assert by_table["digest_queue"]["rows"] == 1  # only the consumed one
    assert _count(engine, "turns") == before


def test_apply_deletes_only_what_the_policy_says(engine: Any) -> None:
    _seed(engine)
    summary = run(engine, now=NOW.timestamp(), apply=True, batch=10, sleep_s=0)
    assert summary["total"] == 10
    # Old operational rows go; the recent ones stay.
    assert _count(engine, "turns", "turn_id = 't-old-800'") == 0
    assert _count(engine, "turns", "turn_id = 't-new-10'") == 1
    assert _count(engine, "turn_attempts", "claimed_at = '2026-10-31T12:00:00+00:00'") == 1
    assert _count(engine, "inbound_messages", "external_id = 'old'") == 0
    assert _count(engine, "inbound_messages", "external_id = 'new'") == 1
    assert _count(engine, "thread_events", "event_id = 'e-old'") == 0
    assert _count(engine, "history", "turn_id = 't-old-800'") == 0
    # Six-year evidence is untouched inside its window.
    assert _count(engine, "messages", "message_id = 'm-evidence'") == 1
    assert _count(engine, "messages", "message_id = 'm-old'") == 0
    assert _count(engine, "proof_events", "event_id = 'pe-new'") == 1
    # A digest item nobody read is never deleted; a consumed one is.
    assert _count(engine, "digest_queue", "body = 'old unread'") == 1
    assert _count(engine, "digest_queue", "body = 'old consumed'") == 0
    assert _count(engine, "digest_queue", "body = 'new consumed'") == 1
    assert _count(engine, "speak_counts", "day = '2026-10-31'") == 1
    assert _count(engine, "routine_runs", "day = '2026-10-31'") == 1


def test_only_one_table_can_be_swept(engine: Any) -> None:
    _seed(engine)
    run(engine, now=NOW.timestamp(), apply=True, only={"turns"}, batch=10, sleep_s=0)
    assert _count(engine, "turns", "turn_id = 't-old-800'") == 0
    assert _count(engine, "thread_events", "event_id = 'e-old'") == 1


def test_max_batches_bounds_one_run(engine: Any) -> None:
    with engine.begin() as conn:
        conn.execute(s.turns.insert(), [
            {"turn_id": f"b-{i}", "tenant_id": "t", "thread_id": "th", "state": "done",
             "created_at": "2024-01-01T00:00:00+00:00", "created_ts": _ts(900 + i)}
            for i in range(3)
        ])
    summary = run(engine, now=NOW.timestamp(), apply=True, only={"turns"},
                  batch=1, max_batches=1, sleep_s=0)
    assert summary["total"] == 1
    assert _count(engine, "turns") == 2


def test_bad_batch_is_refused(engine: Any) -> None:
    with pytest.raises(ValueError):
        run(engine, apply=False, batch=0)
