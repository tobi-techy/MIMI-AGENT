"""Stock estimates with confidence (RAI-294): ``stock_reports``, ``stock_estimates``.

``stock_reports`` holds what shops told Mimi about an item (a count of any
kind, or a buy from another supplier). ``stock_estimates`` is append-only:
one row per recompute with new inputs, always ``proof_level = estimated``.
Downgrade drops both tables.

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
        "stock_reports",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("report_id", sa.String(200), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("product_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("day", sa.Integer(), nullable=False),
        sa.Column("at", sa.String(40), nullable=False),
        sa.Column("bucket", sa.String(8), nullable=True),
        sa.Column("quantity", sa.String(32), nullable=True),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("after_delivery", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint("tenant_id", "report_id"),
    )
    op.create_index("ix_stock_reports_item", "stock_reports",
                    ["tenant_id", "shop_id", "product_id"])
    op.create_table(
        "stock_estimates",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("estimate_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("product_id", sa.String(64), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("as_of_day", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("anchor_qty", sa.Float(), nullable=True),
        sa.Column("anchor_range", sa.String(8), nullable=True),
        sa.Column("anchor_day", sa.Integer(), nullable=True),
        sa.Column("anchor_source", sa.String(32), nullable=False),
        sa.Column("stock_in_since_anchor", sa.Float(), nullable=True),
        sa.Column("est_out_since_anchor", sa.Float(), nullable=True),
        sa.Column("est_left", sa.Float(), nullable=True),
        sa.Column("est_left_raw", sa.Float(), nullable=True),
        sa.Column("est_daily_rate", sa.Float(), nullable=True),
        sa.Column("rate_source", sa.String(32), nullable=False),
        sa.Column("days_to_run_out", sa.Float(), nullable=True),
        sa.Column("run_out_date", sa.String(10), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("confidence_word", sa.String(8), nullable=False),
        sa.Column("money_tied_kobo", sa.BigInteger(), nullable=True),
        sa.Column("proof_level", sa.String(16), nullable=False, server_default="estimated"),
        sa.Column("witnesses", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("proof_policy_version", sa.String(16), nullable=False, server_default="v1"),
        sa.Column("inputs", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("computed_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "estimate_id"),
    )
    op.create_index("ix_stock_estimates_item", "stock_estimates",
                    ["tenant_id", "shop_id", "product_id", "as_of_day"])


def downgrade() -> None:
    op.drop_index("ix_stock_estimates_item", table_name="stock_estimates")
    op.drop_table("stock_estimates")
    op.drop_index("ix_stock_reports_item", table_name="stock_reports")
    op.drop_table("stock_reports")
