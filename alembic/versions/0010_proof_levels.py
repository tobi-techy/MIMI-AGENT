"""Proof levels on every record (RAI-295): verified, likely, estimated.

Adds ``proof_level``, ``witnesses`` (JSON list of kind and source row id) and
``proof_policy_version`` to ``payments``, ``deliveries`` and
``payment_proofs``; adds the append-only ``proof_events`` and the ask-once
``proof_asks`` tables. Existing rows are labelled by the v1 rules from what
the book already holds (``mimi.provenance``), so no record is left
unlabelled:

* payments: matched on the bank feed (and not a fake alert) is verified;
  cash the rep logged is estimated until the shop says YES; a transfer with
  nothing else is the shop's claim, estimated.
* deliveries: the distributor's record alone is estimated; with the shop's
  YES (``witnessed_at``) likely; with a bank-matched payment as well, verified.
* payment screenshots: ``verified`` stays verified (screenshot + bank
  credit); ``likely`` stays likely (screenshot passed the fake check); every
  other state (read, ask, review, duplicate) is the shop's word, estimated.

Downgrade drops the tables and the columns; nothing else is touched.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from mimi.provenance import POLICY_V1, delivery_stamp, payment_stamp, stamp, witness

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

RECORD_TABLES = ("payments", "deliveries", "payment_proofs")


def _cols() -> list[sa.Column]:
    return [
        sa.Column("proof_level", sa.String(16), nullable=False, server_default="estimated"),
        sa.Column("witnesses", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("proof_policy_version", sa.String(16), nullable=False,
                  server_default=POLICY_V1),
    ]


def _backfill(conn: sa.Connection) -> None:
    md = sa.MetaData()
    pay = sa.Table("payments", md, autoload_with=conn)
    dlv = sa.Table("deliveries", md, autoload_with=conn)
    feed = sa.Table("bank_feed", md, autoload_with=conn)
    pp = sa.Table("payment_proofs", md, autoload_with=conn)

    credit_of: dict[tuple[str, str], str] = {
        (r.tenant_id, r.payment_id): r.credit_id
        for r in conn.execute(sa.select(feed.c.tenant_id, feed.c.payment_id, feed.c.credit_id))}
    banked: dict[tuple[str, str], str] = {}
    for p in conn.execute(sa.select(pay)).all():
        credit = credit_of.get((p.tenant_id, p.payment_id), "")
        st = payment_stamp(p.payment_id, p.method, fake_alert=bool(p.fake_alert),
                           bank_credit_id=credit, policy=POLICY_V1)
        if st.proof_level == "verified" and p.delivery_id:
            banked.setdefault((p.tenant_id, p.delivery_id), p.payment_id)
        conn.execute(pay.update().where(pay.c.tenant_id == p.tenant_id,
                                        pay.c.payment_id == p.payment_id).values(**st.columns()))
    for d in conn.execute(sa.select(dlv)).all():
        yes = f"{d.delivery_id}:yes" if d.witnessed_at else ""
        paid = banked.get((d.tenant_id, d.delivery_id), "") if yes else ""
        st = delivery_stamp(d.delivery_id, shop_yes_id=yes, payment_id=paid, policy=POLICY_V1)
        conn.execute(dlv.update().where(dlv.c.tenant_id == d.tenant_id,
                                        dlv.c.delivery_id == d.delivery_id)
                     .values(**st.columns()))
    for r in conn.execute(sa.select(pp)).all():
        ws = [witness("shop_claim", r.proof_id)]
        if r.level in ("likely", "verified"):
            ws = [witness("alert_screenshot_checked", r.proof_id)]
        if r.level == "verified" and r.bank_credit_id:
            ws.append(witness("bank_feed", r.bank_credit_id))
        st = stamp("payment_transfer", ws, POLICY_V1)
        conn.execute(pp.update().where(pp.c.proof_id == r.proof_id).values(**st.columns()))


def upgrade() -> None:
    for table in RECORD_TABLES:
        with op.batch_alter_table(table) as b:
            for c in _cols():
                b.add_column(c)
    op.create_table(
        "proof_events",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("record_type", sa.String(32), nullable=False),
        sa.Column("record_id", sa.String(64), nullable=False),
        sa.Column("old_level", sa.String(16), nullable=False),
        sa.Column("new_level", sa.String(16), nullable=False),
        sa.Column("witness_kind", sa.String(32), nullable=False),
        sa.Column("witness_source", sa.String(200), nullable=False),
        sa.Column("policy_version", sa.String(16), nullable=False),
        sa.Column("at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "event_id"),
    )
    op.create_index("ix_proof_events_record", "proof_events",
                    ["tenant_id", "record_type", "record_id"])
    op.create_table(
        "proof_asks",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("record_type", sa.String(32), nullable=False),
        sa.Column("record_id", sa.String(64), nullable=False),
        sa.Column("asked_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "record_type", "record_id"),
    )
    _backfill(op.get_bind())


def downgrade() -> None:
    op.drop_table("proof_asks")
    op.drop_index("ix_proof_events_record", table_name="proof_events")
    op.drop_table("proof_events")
    for table in reversed(RECORD_TABLES):
        with op.batch_alter_table(table) as b:
            for name in ("proof_policy_version", "witnesses", "proof_level"):
                b.drop_column(name)
