"""Thread compaction versions (RAI-285): one row per summary version.

Raw history is never deleted; each version covers a window of turn ids and
the builder shows the newest version plus the turns after its window. The
version-0 row is the durable timer: a thread whose big turn passed the
threshold compacts once it is due and no turn is queued.

Matches ``mimi/store/schema.py`` (``tests/test_store.py`` checks the two).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "compactions",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("helper", sa.String(32), nullable=False, server_default=""),
        sa.Column("skill", sa.String(64), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary_hash", sa.String(16), nullable=False, server_default=""),
        sa.Column("covered_turns", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_before", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_after", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("due_ts", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("created_at", sa.String(40), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("tenant_id", "thread_id", "version"),
    )


def downgrade() -> None:
    op.drop_table("compactions")
