"""Identity (Phase 1 slice 1): the distributor's people and their territories.

``users`` is the domain user model. Authentication itself arrives later from
an identity provider: ``external_subject`` holds its subject once the person
signs in, and the role always comes from the row here, never from a token.
One owner per tenant is enforced in code (``mimi.identity.store``), not by a
unique index on ``role``, so a disabled former owner can stay on the record.

``user_territories`` links a supervisor to the existing area values
(``shops.area``, ``reps.areas``), trimmed and casefolded by the code that
writes it. It is also how slice 2 will find territory channels per area.

Additive: two new tables, nothing changed on an existing one.

Revision ID: 0021
Revises: 0018 (flips to 0020 when the database agent's 0020 lands on main;
         see docs/IDENTITY.md; exactly one open PR carries a migration at a time)
Create Date: 2026-11-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0021"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("external_subject", sa.String(200), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("phone_e164", sa.String(32), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="invited"),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("last_seen_at", sa.String(40), nullable=True),
        sa.Column("created_by_user_id", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "user_id"),
        sa.UniqueConstraint("tenant_id", "phone_e164", name="uq_users_tenant_phone"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
        sa.UniqueConstraint("tenant_id", "external_subject", name="uq_users_tenant_subject"),
        # Phone only, email only, or both: never neither.
        sa.CheckConstraint("phone_e164 IS NOT NULL OR email IS NOT NULL",
                           name="ck_users_has_identifier"),
    )
    op.create_table(
        "user_territories",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("area", sa.String(200), nullable=False),
        sa.Column("added_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", "area"),
    )
    op.create_index("ix_user_territories_tenant_area", "user_territories",
                    ["tenant_id", "area"])


def downgrade() -> None:
    op.drop_index("ix_user_territories_tenant_area", table_name="user_territories")
    op.drop_table("user_territories")
    op.drop_table("users")
