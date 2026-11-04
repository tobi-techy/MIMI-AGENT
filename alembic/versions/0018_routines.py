"""Scheduled routines (4 Nov 2026): the per-routine switch and the daily claim.

``routine_state`` holds one row per routine and tenant, plus the kill switch
row (routine ``*``, which pauses every routine with no deploy). The pause
survives a restart because it is a row, not a flag in memory.
``routine_runs`` claims one day per routine with its primary key: a restart
or a second API process cannot fire the same routine twice on the same
Africa/Lagos day.

Additive: two new tables, nothing changed on an existing one.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "routine_state",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("routine", sa.String(64), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pause_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("paused_at", sa.String(40), nullable=False, server_default=""),
        sa.Column("last_run_day", sa.String(10), nullable=False, server_default=""),
        sa.Column("last_run_at", sa.String(40), nullable=False, server_default=""),
        sa.Column("last_status", sa.String(16), nullable=False, server_default=""),
        sa.Column("last_finding", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_skip_day", sa.String(10), nullable=False, server_default=""),
        sa.Column("last_missed_day", sa.String(10), nullable=False, server_default=""),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "routine"),
    )
    op.create_table(
        "routine_runs",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("routine", sa.String(64), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("owner", sa.String(160), nullable=False, server_default=""),
        sa.Column("started_at", sa.String(40), nullable=False),
        sa.Column("started_ts", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.String(40), nullable=False, server_default=""),
        sa.Column("findings", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("tenant_id", "routine", "day"),
    )


def downgrade() -> None:
    op.drop_table("routine_runs")
    op.drop_table("routine_state")
