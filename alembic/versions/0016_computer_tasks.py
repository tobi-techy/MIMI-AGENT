"""Computer tasks (RAI-326): one task, its steps and its artifacts.

Revision ID: 0016
Revises: 0015

0016 is the next free number after 0015 (sales tracking) on main.
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "computer_tasks",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("url", sa.String(2000), nullable=False, server_default=""),
        sa.Column("domain", sa.String(255), nullable=False, server_default=""),
        sa.Column("kind", sa.String(32), nullable=False, server_default="generic"),
        sa.Column("status", sa.String(24), nullable=False, server_default="proposed"),
        sa.Column("plan", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("inputs", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("limits", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("engine", sa.String(40), nullable=False, server_default=""),
        sa.Column("stop_reason", sa.String(32), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("created_ts", sa.Float(), nullable=False),
        sa.Column("started_at", sa.String(40), nullable=False, server_default=""),
        sa.Column("finished_at", sa.String(40), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("tenant_id", "task_id"),
    )
    op.create_index("ix_computer_tasks_created", "computer_tasks",
                    ["tenant_id", "created_ts"])
    op.create_table(
        "computer_steps",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("url", sa.String(2000), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("screenshot", sa.String(120), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "task_id", "idx"),
    )
    op.create_table(
        "computer_artifacts",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("mime", sa.String(64), nullable=False, server_default=""),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("path", sa.String(500), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "task_id", "name"),
    )


def downgrade() -> None:
    op.drop_table("computer_artifacts")
    op.drop_table("computer_steps")
    op.drop_index("ix_computer_tasks_created", table_name="computer_tasks")
    op.drop_table("computer_tasks")
