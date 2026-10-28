"""The agent store for the HTTP API (RAI-266): book, threads, turn queue.

The book (tenants, reps, shops, SKUs, deliveries with their lines,
receivables, payments, bank feed, promises, disputes, consent), the agent's
threads, inbound messages (unique per distributor and message id), the
durable turn queue (leases, versions, checkpoints, attempts), messages,
reasoning steps, intents, approvals, credit requests, spend, history,
pinned prefixes and artifacts. Every key starts with ``tenant_id``.

These are exactly the tables in ``mimi/store/schema.py``
(``tests/test_store.py`` checks the two match).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("turn_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("helper", sa.String(32), nullable=False),
        sa.Column("skill", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("tool", sa.String(100), nullable=False),
        sa.Column("intent_kind", sa.String(100), nullable=False),
        sa.Column("preview", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("decided_by", sa.String(100), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("created_ts", sa.Float(), nullable=False),
        sa.Column("decided_at", sa.String(40), nullable=True),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_approvals_tenant_key"),
    )
    op.create_index("ix_approvals_tenant_status", "approvals", ["tenant_id", "status"])
    op.create_table(
        "artifacts",
        sa.Column("path", sa.String(300), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
    )
    op.create_table(
        "asked_once",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("topic", sa.String(100), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "topic", "day"),
    )
    op.create_table(
        "bank_feed",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("credit_id", sa.String(64), nullable=False),
        sa.Column("payment_id", sa.String(64), nullable=False),
        sa.Column("amount_kobo", sa.BigInteger(), nullable=False),
        sa.Column("received_at", sa.String(40), nullable=False),
        sa.Column("narration", sa.String(300), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "credit_id"),
    )
    op.create_table(
        "consent_withheld",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "shop_id", "scope"),
    )
    op.create_table(
        "credit_requests",
        sa.Column("request_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("amount_kobo", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_key", sa.String(100), nullable=False),
        sa.Column("approval_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("tenant_id", "source_key", name="uq_credit_requests_tenant_key"),
    )
    op.create_table(
        "deliveries",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("delivery_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("rep_id", sa.String(64), nullable=False),
        sa.Column("day", sa.Integer(), nullable=False),
        sa.Column("delivered_on", sa.String(10), nullable=False),
        sa.Column("minute", sa.Integer(), nullable=False),
        sa.Column("total_kobo", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("witnessed_at", sa.String(40), nullable=True),
        sa.Column("live", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "delivery_id"),
    )
    op.create_index("ix_deliveries_tenant_shop", "deliveries", ["tenant_id", "shop_id"])
    op.create_table(
        "delivery_lines",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("delivery_id", sa.String(64), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_kobo", sa.BigInteger(), nullable=False),
        sa.Column("line_total_kobo", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "delivery_id", "line_no"),
    )
    op.create_table(
        "disputes",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("dispute_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("delivery_id", sa.String(64), nullable=False),
        sa.Column("day", sa.Integer(), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("missing", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "dispute_id"),
    )
    op.create_table(
        "history",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("turn_id", sa.String(64), nullable=False),
        sa.Column("ts", sa.Float(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "thread_id", "turn_id"),
    )
    op.create_table(
        "inbound_messages",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("turn_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("envelope", sa.Text(), nullable=False),
        sa.Column("media_ref", sa.String(500), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("received_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "external_id"),
    )
    op.create_table(
        "intents",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("attempt_key", sa.String(100), nullable=False),
        sa.Column("turn_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("tool", sa.String(100), nullable=False),
        sa.Column("intent_kind", sa.String(100), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("preview", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "idempotency_key"),
    )
    op.create_table(
        "kill_switches",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("helper", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "helper"),
    )
    op.create_table(
        "messages",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("message_id", sa.String(100), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("turn_id", sa.String(64), nullable=False),
        sa.Column("direction", sa.String(4), nullable=False),
        sa.Column("sender", sa.String(16), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("at", sa.String(40), nullable=False),
        sa.Column("ts", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "message_id"),
    )
    op.create_index("ix_messages_tenant_thread", "messages", ["tenant_id", "thread_id", "ts"])
    op.create_table(
        "payments",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("payment_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("delivery_id", sa.String(64), nullable=False),
        sa.Column("day", sa.Integer(), nullable=False),
        sa.Column("paid_on", sa.String(10), nullable=False),
        sa.Column("amount_kobo", sa.BigInteger(), nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("reference", sa.String(100), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("partial", sa.Boolean(), nullable=False),
        sa.Column("split_of", sa.String(64), nullable=True),
        sa.Column("fake_alert", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "payment_id"),
    )
    op.create_index("ix_payments_tenant_shop", "payments", ["tenant_id", "shop_id"])
    op.create_table(
        "prefixes",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("helper", sa.String(32), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("window_from", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "thread_id", "helper"),
    )
    op.create_table(
        "promises",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("promise_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("delivery_id", sa.String(64), nullable=False),
        sa.Column("made_on", sa.String(10), nullable=False),
        sa.Column("promised_on", sa.String(10), nullable=False),
        sa.Column("kept", sa.Boolean(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "promise_id"),
    )
    op.create_table(
        "receivables",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("delivery_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("due_day", sa.Integer(), nullable=False),
        sa.Column("due_on", sa.String(10), nullable=False),
        sa.Column("total_kobo", sa.BigInteger(), nullable=False),
        sa.Column("paid_kobo", sa.BigInteger(), nullable=False),
        sa.Column("outstanding_kobo", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "delivery_id"),
    )
    op.create_index("ix_receivables_tenant_shop", "receivables", ["tenant_id", "shop_id"])
    op.create_table(
        "reps",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("rep_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("phone", sa.String(32), nullable=False),
        sa.Column("areas", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "rep_id"),
    )
    op.create_table(
        "shops",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("shop_type", sa.String(64), nullable=False),
        sa.Column("size", sa.String(16), nullable=False),
        sa.Column("owner", sa.String(200), nullable=False),
        sa.Column("area", sa.String(200), nullable=False),
        sa.Column("street", sa.String(200), nullable=False),
        sa.Column("phone", sa.String(32), nullable=False),
        sa.Column("rep_id", sa.String(64), nullable=False),
        sa.Column("credit_limit_kobo", sa.BigInteger(), nullable=False),
        sa.Column("bank", sa.String(100), nullable=False),
        sa.Column("restock_days", sa.Integer(), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "shop_id"),
    )
    op.create_index("ix_shops_tenant_phone", "shops", ["tenant_id", "phone"])
    op.create_table(
        "skus",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("brand", sa.String(100), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("price_kobo", sa.BigInteger(), nullable=False),
        sa.Column("risen_price_kobo", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "sku"),
    )
    op.create_table(
        "spend",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("kobo", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "day"),
    )
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("owner_phones", sa.Text(), nullable=False),
        sa.Column("owner_thread", sa.String(200), nullable=False),
        sa.Column("live_date", sa.String(10), nullable=False),
        sa.Column("daily_cap_kobo", sa.BigInteger(), nullable=False),
        sa.Column("collection_account", sa.Text(), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "thread_events",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("event_id", sa.String(100), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("turn_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("tool", sa.String(200), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("short", sa.Text(), nullable=False),
        sa.Column("cost_kobo", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("at", sa.String(40), nullable=False),
        sa.Column("ts", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "event_id"),
    )
    op.create_index("ix_thread_events_tenant_thread", "thread_events", ["tenant_id", "thread_id", "ts"])
    op.create_table(
        "threads",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("claim_seq", sa.Integer(), nullable=False),
        sa.Column("last_at", sa.String(40), nullable=False),
        sa.Column("last_ts", sa.Float(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "thread_id"),
    )
    op.create_table(
        "turn_attempts",
        sa.Column("attempt_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("turn_id", sa.String(64), nullable=False),
        sa.Column("worker_id", sa.String(100), nullable=False),
        sa.Column("takeover", sa.Boolean(), nullable=False),
        sa.Column("claimed_at", sa.String(40), nullable=False),
    )
    op.create_index("ix_turn_attempts_turn_id", "turn_attempts", ["turn_id"])
    op.create_table(
        "turns",
        sa.Column("turn_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("merged_into", sa.String(64), nullable=True),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        sa.Column("lease_expires_ts", sa.Float(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("completions", sa.Integer(), nullable=False),
        sa.Column("batch", sa.Text(), nullable=False),
        sa.Column("checkpoint", sa.Text(), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("helper", sa.String(32), nullable=False),
        sa.Column("skill", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("stop", sa.Boolean(), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("created_ts", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.String(40), nullable=True),
    )
    op.create_index("ix_turns_state", "turns", ["state", "created_ts"])
    op.create_index("ix_turns_tenant_thread", "turns", ["tenant_id", "thread_id"])


def downgrade() -> None:
    op.drop_table("turns")
    op.drop_table("turn_attempts")
    op.drop_table("threads")
    op.drop_table("thread_events")
    op.drop_table("tenants")
    op.drop_table("spend")
    op.drop_table("skus")
    op.drop_table("shops")
    op.drop_table("reps")
    op.drop_table("receivables")
    op.drop_table("promises")
    op.drop_table("prefixes")
    op.drop_table("payments")
    op.drop_table("messages")
    op.drop_table("kill_switches")
    op.drop_table("intents")
    op.drop_table("inbound_messages")
    op.drop_table("history")
    op.drop_table("disputes")
    op.drop_table("delivery_lines")
    op.drop_table("deliveries")
    op.drop_table("credit_requests")
    op.drop_table("consent_withheld")
    op.drop_table("bank_feed")
    op.drop_table("asked_once")
    op.drop_table("artifacts")
    op.drop_table("approvals")
