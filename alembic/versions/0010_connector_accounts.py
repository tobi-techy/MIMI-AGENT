"""Connector accounts, sync runs, write audit and consent grants (RAI-277).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-01

``connector_accounts`` holds no tokens: Composio holds them.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_accounts",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("connection_id", sa.String(64), nullable=False),
        sa.Column("toolkit", sa.String(32), nullable=False),
        sa.Column("connected_account_id", sa.String(100), nullable=False, server_default=""),
        sa.Column("region", sa.String(8), nullable=False, server_default="com"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("connected_at", sa.String(40), nullable=False),
        sa.Column("revoked_at", sa.String(40), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "connection_id"),
    )
    op.create_table(
        "connector_sync_runs",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("connection_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("scope", sa.String(32), nullable=False, server_default="stock"),
        sa.Column("rows_read", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_mapped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("started_at", sa.String(40), nullable=False),
        sa.Column("finished_at", sa.String(40), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "run_id"),
    )
    op.create_index("ix_connector_sync_runs_conn", "connector_sync_runs",
                    ["tenant_id", "connection_id"])
    op.create_table(
        "connector_writes",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("write_id", sa.String(64), nullable=False),
        sa.Column("proposal_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("before", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("after", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("external_id", sa.String(100), nullable=False, server_default=""),
        sa.Column("approval_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("applied_at", sa.String(40), nullable=False),
        sa.Column("undone_at", sa.String(40), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "write_id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key",
                            name="uq_connector_writes_tenant_key"),
    )
    op.create_table(
        "consent_grants",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("receiver", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("granted_at", sa.String(40), nullable=False),
        sa.Column("revoked_at", sa.String(40), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "shop_id", "receiver", "scope", "granted_at"),
    )
    op.create_index("ix_consent_grants_shop", "consent_grants",
                    ["tenant_id", "shop_id", "scope"])
    op.create_table(
        "stock_counts",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("item", sa.String(200), nullable=False, server_default=""),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("counted_on", sa.String(10), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="connector"),
        sa.PrimaryKeyConstraint("tenant_id", "shop_id", "sku", "counted_on", "source"),
    )


def downgrade() -> None:
    op.drop_table("stock_counts")
    op.drop_index("ix_consent_grants_shop", table_name="consent_grants")
    op.drop_table("consent_grants")
    op.drop_table("connector_writes")
    op.drop_index("ix_connector_sync_runs_conn", table_name="connector_sync_runs")
    op.drop_table("connector_sync_runs")
    op.drop_table("connector_accounts")
