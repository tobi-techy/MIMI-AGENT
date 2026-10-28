"""Per-thread inbox and draft versions (RAI-301).

``thread_inbox`` holds a message that arrived while its thread's turn was
running, until the running loop reads it at a step boundary.
``approvals.supersedes`` and ``approvals.superseded_by`` link a corrected
draft to the pending one it replaced. ``turns.solo`` marks a follow-up turn
for one part of a message, which is never merged with other turns.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "thread_inbox",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("turn_id", sa.String(64), nullable=False),
        sa.Column("running_turn_id", sa.String(64), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("taken_by", sa.String(64), nullable=False),
        sa.Column("steer", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("created_ts", sa.Float(), nullable=False),
        sa.Column("taken_at", sa.String(40), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "external_id"),
    )
    op.create_index("ix_thread_inbox_thread", "thread_inbox",
                    ["tenant_id", "thread_id", "status"])
    with op.batch_alter_table("approvals") as b:
        b.add_column(sa.Column("supersedes", sa.String(64), nullable=True))
        b.add_column(sa.Column("superseded_by", sa.String(64), nullable=True))
    with op.batch_alter_table("turns") as b:
        b.add_column(sa.Column("solo", sa.Boolean(), nullable=False,
                               server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("turns") as b:
        b.drop_column("solo")
    with op.batch_alter_table("approvals") as b:
        b.drop_column("superseded_by")
        b.drop_column("supersedes")
    op.drop_index("ix_thread_inbox_thread", table_name="thread_inbox")
    op.drop_table("thread_inbox")
