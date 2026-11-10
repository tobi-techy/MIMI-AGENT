"""Indexes for the hot request paths, measured on pilot data (10k shops).

Every index here is named in ``docs/DATA.md`` with the query it serves and the
before and after plan on a pilot-scale database. No column, table or constraint
changes: this revision can be deployed while the previous build still runs.

Three indexes are **replaced** rather than added, because the new one starts
with the old one's columns: the old one would only add a write cost. Each drop
is an index drop (no data is lost) and its downgrade recreates it.

On Postgres every index builds ``CONCURRENTLY`` in an autocommit block, so a
deploy does not hold a write lock on a growing table. SQLite has no such
option and builds inline; the offline tests and a local try use it.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-10
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

#: The statements are built by the helpers below (SQLite and Postgres spell a
#: concurrent index differently). Every build and drop goes through ``_run``,
#: which uses an autocommit block with CONCURRENTLY on Postgres.
REVIEWED_SQL = "index builds and drops only, all CONCURRENTLY on Postgres"

#: (name, table, columns, partial predicate).
NEW_INDEXES: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    # The console inbox: threads newest first (ORDER BY last_ts, thread_id).
    ("ix_threads_tenant_last", "threads", ("tenant_id", "last_ts", "thread_id"), ""),
    # Tasks and Activity: a tenant's turns newest first. Not a claim index:
    # claiming filters state and lease and is served by ix_turns_state.
    ("ix_turns_tenant_created", "turns", ("tenant_id", "created_ts"), ""),
    # An approval's draft message, read once per approval in the list.
    ("ix_messages_tenant_idem", "messages", ("tenant_id", "idempotency_key"), ""),
    # A thread's waiting approvals: the inbox summary counts them per row.
    ("ix_approvals_tenant_thread_status", "approvals",
     ("tenant_id", "thread_id", "status"), ""),
    # The Activity view reads one turn's steps without naming its thread.
    ("ix_thread_events_tenant_turn", "thread_events", ("tenant_id", "turn_id"), ""),
    # The queue claim joins a candidate turn to its inbound message.
    ("ix_inbound_messages_tenant_turn", "inbound_messages", ("tenant_id", "turn_id"), ""),
    # A thread's uploads, read back in demo mode by thread and media prefix.
    ("ix_inbound_messages_tenant_thread", "inbound_messages", ("tenant_id", "thread_id"), ""),
    # Open invoices in book order; paid rows leave the index.
    ("ix_receivables_tenant_open", "receivables",
     ("tenant_id", "position", "delivery_id"), "outstanding_kobo > 0"),
    # The morning brief and dashboard: the tenant's last days of payments.
    ("ix_payments_tenant_day", "payments", ("tenant_id", "day"), ""),
    # Unbacked transfer alerts only (the brief reads the exceptions).
    ("ix_payments_tenant_fake_day", "payments", ("tenant_id", "day"),
     "fake_alert IS TRUE"),
    # Matching an inbound transfer to its payment by amount.
    ("ix_bank_feed_tenant_amount", "bank_feed", ("tenant_id", "amount_kobo"), ""),
    # One shop's disputes and its open count.
    ("ix_disputes_tenant_shop_status", "disputes", ("tenant_id", "shop_id", "status"), ""),
    # A shop's promises on its page.
    ("ix_promises_tenant_shop", "promises", ("tenant_id", "shop_id"), ""),
    # A record's proof history, in the order it landed.
    ("ix_proof_events_tenant_record", "proof_events", ("tenant_id", "record_id", "at"), ""),
    # A thread's history in time order (the context builder and compactor).
    ("ix_history_tenant_thread_ts", "history", ("tenant_id", "thread_id", "ts"), ""),
    # Due compaction timers only, one entry per waiting thread.
    ("ix_compactions_due", "compactions", ("due_ts",), "version = 0"),
)

#: (new name, table, columns, old name): the new index starts with the old
#: one's columns, so the old one is dropped in the same revision.
REPLACED: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("ix_approvals_tenant_status_created", "approvals",
     ("tenant_id", "status", "created_ts"), "ix_approvals_tenant_status"),
    ("ix_payments_tenant_shop_day", "payments",
     ("tenant_id", "shop_id", "day"), "ix_payments_tenant_shop"),
    ("ix_deliveries_tenant_shop_day", "deliveries",
     ("tenant_id", "shop_id", "day", "delivery_id"), "ix_deliveries_tenant_shop"),
)


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def _run(sql: str) -> None:
    """One statement; Postgres index builds run outside a transaction."""
    if _is_postgres():
        with op.get_context().autocommit_block():
            op.execute(sql)
    else:
        op.execute(sql)


def _create(name: str, table: str, cols: tuple[str, ...], where: str = "") -> None:
    sql = (f"CREATE INDEX {'CONCURRENTLY ' if _is_postgres() else ''}{name} "
           f"ON {table} ({', '.join(cols)})")
    if where:
        sql += f" WHERE {where}"
    _run(sql)


def _drop(name: str) -> None:
    _run(f"DROP INDEX {'CONCURRENTLY ' if _is_postgres() else ''}IF EXISTS {name}")


def upgrade() -> None:
    for name, table, cols, where in NEW_INDEXES:
        _create(name, table, cols, where)
    for new, table, cols, old in REPLACED:
        _create(new, table, cols)
        _drop(old)


def downgrade() -> None:
    for new, _table, _cols, _old in REPLACED:
        _drop(new)
    for name, _table, _cols, _where in NEW_INDEXES:
        _drop(name)
    # The three indexes 0019 replaced, exactly as 0006/0002 created them.
    _create("ix_deliveries_tenant_shop", "deliveries", ("tenant_id", "shop_id"))
    _create("ix_payments_tenant_shop", "payments", ("tenant_id", "shop_id"))
    _create("ix_approvals_tenant_status", "approvals", ("tenant_id", "status"))
