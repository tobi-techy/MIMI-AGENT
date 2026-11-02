"""Digest queue and per-tenant speak limits (RAI-300).

``digest_queue`` is the owner's digest queue the daily note (RAI-289)
reads; ``speak_counts`` is one row per unprompted message, so the
per-audience daily cap counts across restarts and workers. Matches
``mimi/store/schema.py`` (``tests/test_store.py`` checks the two).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "digest_queue",
        sa.Column("item_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("kind", sa.String(32), nullable=False, server_default="reply"),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("consumed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.String(40), nullable=False, server_default=""),
    )
    op.create_index("ix_digest_queue_tenant_day", "digest_queue",
                    ["tenant_id", "day", "consumed"])
    op.create_table(
        "speak_counts",
        sa.Column("count_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("audience", sa.String(16), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False, server_default=""),
    )
    op.create_index("ix_speak_counts_tenant_day", "speak_counts",
                    ["tenant_id", "audience", "shop_id", "day"])


def downgrade() -> None:
    op.drop_index("ix_speak_counts_tenant_day", table_name="speak_counts")
    op.drop_table("speak_counts")
    op.drop_index("ix_digest_queue_tenant_day", table_name="digest_queue")
    op.drop_table("digest_queue")
