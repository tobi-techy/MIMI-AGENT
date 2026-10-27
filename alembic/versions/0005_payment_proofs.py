"""Payment screenshots as fields, never pixels (RAI-268).

One row per screenshot message: sender and time, the image's SHA-256 and
size, what Muse Spark copied off it, the verdict and its reasons, the
reference, the invoice it matched and the bank credit that later witnessed
it. The image bytes are never stored.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-27
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_proofs",
        sa.Column("proof_id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("shop_id", sa.String(), nullable=False),
        sa.Column("sender", sa.String(), nullable=False),
        sa.Column("received_at", sa.String(), nullable=False),
        sa.Column("image_sha256", sa.String(), nullable=False),
        sa.Column("image_bytes", sa.Integer(), nullable=False),
        sa.Column("reading_json", sa.Text(), nullable=False),
        sa.Column("read_via", sa.String(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("level", sa.String(), nullable=False, server_default="read"),
        sa.Column("verdict", sa.String(), nullable=False, server_default=""),
        sa.Column("reasons_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("amount_kobo", sa.BigInteger(), nullable=True),
        sa.Column("paid_at", sa.String(), nullable=False, server_default=""),
        sa.Column("reference", sa.String(), nullable=False, server_default=""),
        sa.Column("session_id", sa.String(), nullable=False, server_default=""),
        sa.Column("delivery_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("bank_credit_id", sa.String(), nullable=False, server_default=""),
    )
    op.create_index("ix_payment_proofs_shop_id", "payment_proofs", ["shop_id"])
    op.create_index("ix_payment_proofs_image_sha256", "payment_proofs", ["image_sha256"])
    op.create_index("ix_payment_proofs_reference", "payment_proofs", ["reference"])
    op.create_index("ix_payment_proofs_session_id", "payment_proofs", ["session_id"])


def downgrade() -> None:
    for name in ("session_id", "reference", "image_sha256", "shop_id"):
        op.drop_index(f"ix_payment_proofs_{name}", table_name="payment_proofs")
    op.drop_table("payment_proofs")
