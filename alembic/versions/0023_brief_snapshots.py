"""The daily brief's own numbers, so the next brief can say what changed.

The brief (``mimi.api.brief``) counts the book every morning. To lead with the
movement the owner always asks about, it needs one thing the book does not
hold: what the same figures were yesterday. This is that memory, one row per
tenant and Lagos day.

Nothing else reads it. It is not the book, no receivable or payment is touched,
and a missing row (the first brief, or a day the routine did not run) only means
that brief says "first brief" instead of inventing a comparison.

Additive and reversible: one table. It is created empty in the same deploy, so
the index build and the NOT NULL columns are on a table with no rows.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brief_snapshots",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("book_day", sa.String(10), nullable=False),
        sa.Column("at_risk_kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("overdue_kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("collected_kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("returns_open", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("returns_value_kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("returns_closed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shops_missed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_listed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("collection_targets", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "day"),
    )


def downgrade() -> None:
    op.drop_table("brief_snapshots")
