"""Demo mode: visitor sessions and the spend of swept sessions (console phase 1).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "demo_sessions",
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("session_tenant", sa.String(64), nullable=False),
        sa.Column("ip_hash", sa.String(64), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("created_ts", sa.Float(), nullable=False),
        sa.Column("expires_ts", sa.Float(), nullable=False),
        sa.Column("swept", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint("token_digest"),
    )
    op.create_index("ix_demo_sessions_ip", "demo_sessions", ["ip_hash", "day"])
    op.create_index("ix_demo_sessions_tenant", "demo_sessions", ["session_tenant"])
    op.create_table(
        "demo_usage",
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("ip_hash", sa.String(64), nullable=False),
        sa.Column("kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("day", "ip_hash"),
    )


def downgrade() -> None:
    op.drop_table("demo_usage")
    op.drop_index("ix_demo_sessions_tenant", table_name="demo_sessions")
    op.drop_index("ix_demo_sessions_ip", table_name="demo_sessions")
    op.drop_table("demo_sessions")
