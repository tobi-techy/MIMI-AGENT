"""Trace columns for the observability track (11 Nov 2026).

Two nullable columns and one index, nothing else:

* ``connector_writes.trace_id``: a connector write can be found by the trace
  that made it without joining ``approval_id -> approvals.turn_id``, which
  breaks the day a write has no approval behind it. Indexed with
  ``tenant_id`` because every read is tenant-scoped.
* ``digest_queue.trace_id``: the turn or routine that queued a digest item.

Both are nullable, and stay nullable in this revision: history predates them,
and nullable plus an index is the expand half of expand and contract. The NOT
NULL constraint comes in a later revision, only once every writer sets it.

``runtime_jobs.tenant_id`` needed no change: it has existed since 0001,
``NOT NULL`` and indexed (``ix_runtime_jobs_tenant_id``).

Additive and reversible. The Postgres index build is ``CONCURRENTLY`` in an
autocommit block; the columns are nullable, so adding them is a catalogue-only
change with no table rewrite.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-11
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

#: Every statement here is one of the three below, all through ``_run``.
REVIEWED_SQL = "two nullable ADD COLUMNs and one CONCURRENT index on Postgres"


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def _run(sql: str) -> None:
    if _is_postgres():
        with op.get_context().autocommit_block():
            op.execute(sql)
    else:
        op.execute(sql)


def upgrade() -> None:
    op.add_column("connector_writes",
                  sa.Column("trace_id", sa.String(64), nullable=True))
    op.add_column("digest_queue",
                  sa.Column("trace_id", sa.String(64), nullable=True))
    _run(f"CREATE INDEX {'CONCURRENTLY ' if _is_postgres() else ''}"
         "ix_connector_writes_tenant_trace ON connector_writes (tenant_id, trace_id)")


def downgrade() -> None:
    _run(f"DROP INDEX {'CONCURRENTLY ' if _is_postgres() else ''}"
         "IF EXISTS ix_connector_writes_tenant_trace")
    op.drop_column("digest_queue", "trace_id")
    op.drop_column("connector_writes", "trace_id")
