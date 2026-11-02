"""The distributor's rulebook (RAI-323): policy rules, their change log, and
hand-off approvals.

Revision ID: 0014
Revises: 0013

Renumbered at integration (2026-11-02): was 0010 on 0009; 0010 to 0013 are
proof levels, connectors, compactions and speak (RAI-295, 277, 285, 300).
Create Date: 2026-11-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policy_rules",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("behaviour", sa.String(24), nullable=False),
        sa.Column("updated_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "action_type"),
    )
    op.create_table(
        "policy_changes",
        sa.Column("change_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("old_behaviour", sa.String(24), nullable=False),
        sa.Column("new_behaviour", sa.String(24), nullable=False),
        sa.Column("changed_by", sa.String(100), nullable=False),
        sa.Column("key_digest", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("created_ts", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("change_id"),
    )
    op.create_index("ix_policy_changes_tenant", "policy_changes", ["tenant_id", "created_ts"])
    with op.batch_alter_table("approvals") as b:
        b.add_column(sa.Column("handling", sa.String(16), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("approvals") as b:
        b.drop_column("handling")
    op.drop_index("ix_policy_changes_tenant", table_name="policy_changes")
    op.drop_table("policy_changes")
    op.drop_table("policy_rules")
