"""The agent store's tables (RAI-266): the book, the threads, the turn queue.

One SQLAlchemy ``MetaData`` for everything the HTTP API and its workers keep
between requests and across restarts. SQLite by default, Postgres through
``DATABASE_URL``. Alembic revision ``0006_api_store`` creates exactly these
tables (``tests/test_store.py`` compares the two).

Every row carries ``tenant_id`` and every primary or unique key starts with
it, so two distributors can use the same shop id or message id and never
meet. Money is integer kobo. Times are ISO 8601 strings with their zone
(UTC, ``+00:00``); the ``*_ts`` float columns beside some of them are epoch
seconds for ordering and leases only.
"""

from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()


def _tenant() -> sa.Column[str]:
    return sa.Column("tenant_id", sa.String(64), nullable=False)


# ------------------------------------------------------------------ the book
# Loaded by ``python -m mimi.store seed`` from the synthetic world. In
# production Go owns these; ``SqlGo`` is the port the harness reads them by.

tenants = sa.Table(
    "tenants", metadata,
    sa.Column("tenant_id", sa.String(64), primary_key=True),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("owner_phones", sa.Text(), nullable=False, default="[]"),  # JSON list
    sa.Column("owner_thread", sa.String(200), nullable=False, default=""),
    # The book's "today" (Africa/Lagos calendar day), ISO date.
    sa.Column("live_date", sa.String(10), nullable=False),
    # The world's day number of ``live_date`` and the day some prices rose
    # (alembic 0007): the stock and credit helpers price and date in days.
    sa.Column("live_day", sa.Integer(), nullable=False, default=0),
    sa.Column("price_rise_day", sa.Integer(), nullable=True),
    sa.Column("daily_cap_kobo", sa.BigInteger(), nullable=False, default=50_000),
    sa.Column("collection_account", sa.Text(), nullable=False, default="{}"),  # JSON
    sa.Column("simulated", sa.Boolean(), nullable=False, default=True),
    sa.Column("created_at", sa.String(40), nullable=False),
)

reps = sa.Table(
    "reps", metadata,
    _tenant(),
    sa.Column("rep_id", sa.String(64), nullable=False),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("phone", sa.String(32), nullable=False),
    sa.Column("areas", sa.Text(), nullable=False, default="[]"),
    sa.PrimaryKeyConstraint("tenant_id", "rep_id"),
)

shops = sa.Table(
    "shops", metadata,
    _tenant(),
    sa.Column("shop_id", sa.String(64), nullable=False),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("shop_type", sa.String(64), nullable=False, default=""),
    sa.Column("size", sa.String(16), nullable=False, default=""),
    sa.Column("owner", sa.String(200), nullable=False, default=""),
    sa.Column("area", sa.String(200), nullable=False, default=""),
    sa.Column("street", sa.String(200), nullable=False, default=""),
    sa.Column("phone", sa.String(32), nullable=False),
    sa.Column("rep_id", sa.String(64), nullable=False, default=""),
    sa.Column("credit_limit_kobo", sa.BigInteger(), nullable=False, default=0),
    sa.Column("bank", sa.String(100), nullable=False, default=""),
    sa.Column("restock_days", sa.Integer(), nullable=False, default=7),
    sa.Column("simulated", sa.Boolean(), nullable=False, default=True),
    sa.PrimaryKeyConstraint("tenant_id", "shop_id"),
    sa.Index("ix_shops_tenant_phone", "tenant_id", "phone"),
)

consent_withheld = sa.Table(
    "consent_withheld", metadata,
    _tenant(),
    sa.Column("shop_id", sa.String(64), nullable=False),
    sa.Column("scope", sa.String(64), nullable=False),  # "shop:*" withholds all
    sa.PrimaryKeyConstraint("tenant_id", "shop_id", "scope"),
)

skus = sa.Table(
    "skus", metadata,
    _tenant(),
    sa.Column("sku", sa.String(64), nullable=False),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("brand", sa.String(100), nullable=False, default=""),
    sa.Column("unit", sa.String(32), nullable=False, default=""),
    sa.Column("price_kobo", sa.BigInteger(), nullable=False),
    sa.Column("risen_price_kobo", sa.BigInteger(), nullable=True),
    sa.PrimaryKeyConstraint("tenant_id", "sku"),
)

deliveries = sa.Table(
    "deliveries", metadata,
    _tenant(),
    sa.Column("delivery_id", sa.String(64), nullable=False),
    sa.Column("shop_id", sa.String(64), nullable=False),
    sa.Column("rep_id", sa.String(64), nullable=False, default=""),
    sa.Column("day", sa.Integer(), nullable=False),
    sa.Column("delivered_on", sa.String(10), nullable=False),
    sa.Column("minute", sa.Integer(), nullable=False, default=0),  # after midnight, WAT
    # Computed by code from the lines when the row is written.
    sa.Column("total_kobo", sa.BigInteger(), nullable=False),
    sa.Column("status", sa.String(32), nullable=False, default="delivered"),
    sa.Column("witnessed_at", sa.String(40), nullable=True),
    sa.Column("live", sa.Boolean(), nullable=False, default=False),
    sa.PrimaryKeyConstraint("tenant_id", "delivery_id"),
    sa.Index("ix_deliveries_tenant_shop", "tenant_id", "shop_id"),
)

delivery_lines = sa.Table(
    "delivery_lines", metadata,
    _tenant(),
    sa.Column("delivery_id", sa.String(64), nullable=False),
    sa.Column("line_no", sa.Integer(), nullable=False),
    sa.Column("sku", sa.String(64), nullable=False),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("unit", sa.String(32), nullable=False, default=""),
    sa.Column("quantity", sa.Integer(), nullable=False),
    sa.Column("unit_price_kobo", sa.BigInteger(), nullable=False),
    sa.Column("line_total_kobo", sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "delivery_id", "line_no"),
)

receivables = sa.Table(
    "receivables", metadata,
    _tenant(),
    sa.Column("delivery_id", sa.String(64), nullable=False),
    sa.Column("shop_id", sa.String(64), nullable=False),
    sa.Column("due_day", sa.Integer(), nullable=False),
    sa.Column("due_on", sa.String(10), nullable=False),
    sa.Column("total_kobo", sa.BigInteger(), nullable=False),
    sa.Column("paid_kobo", sa.BigInteger(), nullable=False),
    sa.Column("outstanding_kobo", sa.BigInteger(), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    # The book's own order (oldest first), so lists read the same everywhere.
    sa.Column("position", sa.Integer(), nullable=False, default=0),
    sa.PrimaryKeyConstraint("tenant_id", "delivery_id"),
    sa.Index("ix_receivables_tenant_shop", "tenant_id", "shop_id"),
)

payments = sa.Table(
    "payments", metadata,
    _tenant(),
    sa.Column("payment_id", sa.String(64), nullable=False),
    sa.Column("shop_id", sa.String(64), nullable=False),
    sa.Column("delivery_id", sa.String(64), nullable=False, default=""),
    sa.Column("day", sa.Integer(), nullable=False),
    sa.Column("paid_on", sa.String(10), nullable=False),
    sa.Column("amount_kobo", sa.BigInteger(), nullable=False),
    sa.Column("method", sa.String(32), nullable=False),
    sa.Column("reference", sa.String(100), nullable=False, default=""),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("partial", sa.Boolean(), nullable=False, default=False),
    sa.Column("split_of", sa.String(64), nullable=True),
    # A fake transfer alert: no money arrived; never counts as paid.
    sa.Column("fake_alert", sa.Boolean(), nullable=False, default=False),
    sa.PrimaryKeyConstraint("tenant_id", "payment_id"),
    sa.Index("ix_payments_tenant_shop", "tenant_id", "shop_id"),
)

bank_feed = sa.Table(
    "bank_feed", metadata,
    _tenant(),
    sa.Column("credit_id", sa.String(64), nullable=False),
    sa.Column("payment_id", sa.String(64), nullable=False),
    sa.Column("amount_kobo", sa.BigInteger(), nullable=False),
    sa.Column("received_at", sa.String(40), nullable=False),
    sa.Column("narration", sa.String(300), nullable=False, default=""),
    sa.PrimaryKeyConstraint("tenant_id", "credit_id"),
)

promises = sa.Table(
    "promises", metadata,
    _tenant(),
    sa.Column("promise_id", sa.String(64), nullable=False),
    sa.Column("shop_id", sa.String(64), nullable=False),
    sa.Column("delivery_id", sa.String(64), nullable=False, default=""),
    sa.Column("made_on", sa.String(10), nullable=False),
    sa.Column("promised_on", sa.String(10), nullable=False),
    sa.Column("kept", sa.Boolean(), nullable=False, default=False),
    sa.Column("text", sa.Text(), nullable=False, default=""),
    sa.PrimaryKeyConstraint("tenant_id", "promise_id"),
)

disputes = sa.Table(
    "disputes", metadata,
    _tenant(),
    sa.Column("dispute_id", sa.String(64), nullable=False),
    sa.Column("shop_id", sa.String(64), nullable=False),
    sa.Column("delivery_id", sa.String(64), nullable=False, default=""),
    sa.Column("day", sa.Integer(), nullable=False),
    sa.Column("sku", sa.String(64), nullable=False, default=""),
    sa.Column("missing", sa.Integer(), nullable=False, default=0),
    sa.Column("text", sa.Text(), nullable=False, default=""),
    sa.Column("status", sa.String(16), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "dispute_id"),
)

# ------------------------------------------------------------- the agent

threads = sa.Table(
    "threads", metadata,
    _tenant(),
    sa.Column("thread_id", sa.String(200), nullable=False),
    sa.Column("kind", sa.String(16), nullable=False),  # shop | owner | rep | job | new
    sa.Column("shop_id", sa.String(64), nullable=False, default=""),
    # The router's ThreadState (a helper waiting on an answer, a clarify).
    sa.Column("state", sa.Text(), nullable=False, default="{}"),
    sa.Column("summary", sa.Text(), nullable=False, default=""),
    # Bumped by every claim on the thread: serialises claims per thread.
    sa.Column("claim_seq", sa.Integer(), nullable=False, default=0),
    sa.Column("last_at", sa.String(40), nullable=False),
    sa.Column("last_ts", sa.Float(), nullable=False),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "thread_id"),
)

inbound_messages = sa.Table(
    "inbound_messages", metadata,
    _tenant(),
    # The provider's message id (WhatsApp wamid, console id): the dedupe key.
    sa.Column("external_id", sa.String(200), nullable=False),
    sa.Column("turn_id", sa.String(64), nullable=False),
    sa.Column("thread_id", sa.String(200), nullable=False),
    sa.Column("source", sa.String(32), nullable=False),
    # The ``Inbound`` envelope as JSON; the phone is blanked once the turn is done.
    sa.Column("envelope", sa.Text(), nullable=False),
    sa.Column("media_ref", sa.String(500), nullable=False, default=""),
    sa.Column("simulated", sa.Boolean(), nullable=False, default=False),
    sa.Column("received_at", sa.String(40), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "external_id"),
)

turns = sa.Table(
    "turns", metadata,
    sa.Column("turn_id", sa.String(64), primary_key=True),
    _tenant(),
    sa.Column("thread_id", sa.String(200), nullable=False),
    # queued | leased | done | failed | merged (into another turn's batch)
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("merged_into", sa.String(64), nullable=True),
    sa.Column("lease_owner", sa.String(100), nullable=True),
    sa.Column("lease_expires_ts", sa.Float(), nullable=True),
    sa.Column("version", sa.Integer(), nullable=False, default=0),
    sa.Column("attempts", sa.Integer(), nullable=False, default=0),
    # Incremented only by the completing compare-and-set: must end at 1.
    sa.Column("completions", sa.Integer(), nullable=False, default=0),
    # External ids of the inbound batch this turn covers (fixed at first claim).
    sa.Column("batch", sa.Text(), nullable=False, default="[]"),
    # Route decision and harness loop state after the last saved step.
    sa.Column("checkpoint", sa.Text(), nullable=False, default="{}"),
    sa.Column("status", sa.String(64), nullable=False, default=""),
    sa.Column("helper", sa.String(32), nullable=False, default=""),
    sa.Column("skill", sa.String(64), nullable=False, default=""),
    sa.Column("run_id", sa.String(64), nullable=False, default=""),
    sa.Column("result", sa.Text(), nullable=False, default="{}"),
    sa.Column("error", sa.Text(), nullable=False, default=""),
    # The owner said "stop": the turn ends at its next step boundary.
    sa.Column("stop", sa.Boolean(), nullable=False, default=False),
    sa.Column("simulated", sa.Boolean(), nullable=False, default=False),
    # A follow-up turn for one part of a message (RAI-301): it runs on its
    # own, never merged with other waiting turns and never leading a merge.
    sa.Column("solo", sa.Boolean(), nullable=False, default=False),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("created_ts", sa.Float(), nullable=False),
    sa.Column("finished_at", sa.String(40), nullable=True),
    sa.Index("ix_turns_state", "state", "created_ts"),
    sa.Index("ix_turns_tenant_thread", "tenant_id", "thread_id"),
)

turn_attempts = sa.Table(
    "turn_attempts", metadata,
    sa.Column("attempt_id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("turn_id", sa.String(64), nullable=False, index=True),
    sa.Column("worker_id", sa.String(100), nullable=False),
    sa.Column("takeover", sa.Boolean(), nullable=False, default=False),
    sa.Column("claimed_at", sa.String(40), nullable=False),
)

messages = sa.Table(
    "messages", metadata,
    _tenant(),
    sa.Column("message_id", sa.String(100), nullable=False),
    sa.Column("thread_id", sa.String(200), nullable=False),
    sa.Column("turn_id", sa.String(64), nullable=False, default=""),
    sa.Column("direction", sa.String(4), nullable=False),  # in | out
    sa.Column("sender", sa.String(16), nullable=False),  # shop|owner|rep|mimi|system
    sa.Column("channel", sa.String(32), nullable=False, default=""),
    sa.Column("shop_id", sa.String(64), nullable=False, default=""),
    sa.Column("text", sa.Text(), nullable=False, default=""),
    sa.Column("kind", sa.String(32), nullable=False, default=""),
    # in: received. out: ready | pending_approval | rejected | sent
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("idempotency_key", sa.String(100), nullable=False, default=""),
    sa.Column("simulated", sa.Boolean(), nullable=False, default=False),
    sa.Column("at", sa.String(40), nullable=False),
    sa.Column("ts", sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "message_id"),
    sa.Index("ix_messages_tenant_thread", "tenant_id", "thread_id", "ts"),
)

thread_events = sa.Table(
    "thread_events", metadata,
    _tenant(),
    sa.Column("event_id", sa.String(100), nullable=False),
    sa.Column("thread_id", sa.String(200), nullable=False),
    sa.Column("run_id", sa.String(64), nullable=False, default=""),
    sa.Column("turn_id", sa.String(64), nullable=False, default=""),
    sa.Column("seq", sa.Integer(), nullable=False, default=0),
    sa.Column("step", sa.Integer(), nullable=False, default=0),
    sa.Column("type", sa.String(32), nullable=False),
    # Every tool a step called, comma separated: unbounded.
    sa.Column("tool", sa.Text(), nullable=False, default=""),
    sa.Column("tier", sa.String(16), nullable=False, default=""),
    sa.Column("short", sa.Text(), nullable=False, default=""),
    sa.Column("cost_kobo", sa.BigInteger(), nullable=False, default=0),
    sa.Column("model", sa.String(100), nullable=False, default=""),
    sa.Column("duration_ms", sa.Float(), nullable=False, default=0.0),
    sa.Column("data", sa.Text(), nullable=False, default="{}"),
    sa.Column("at", sa.String(40), nullable=False),
    sa.Column("ts", sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "event_id"),
    sa.Index("ix_thread_events_tenant_thread", "tenant_id", "thread_id", "ts"),
)

intents = sa.Table(
    "intents", metadata,
    _tenant(),
    # The business key: one row however many times it is proposed.
    sa.Column("idempotency_key", sa.String(100), nullable=False),
    sa.Column("attempt_key", sa.String(100), nullable=False, default=""),
    sa.Column("turn_id", sa.String(64), nullable=False, default=""),
    sa.Column("thread_id", sa.String(200), nullable=False, default=""),
    sa.Column("tool", sa.String(100), nullable=False),
    sa.Column("intent_kind", sa.String(100), nullable=False),
    sa.Column("tier", sa.String(16), nullable=False),
    sa.Column("shop_id", sa.String(64), nullable=False, default=""),
    sa.Column("payload", sa.Text(), nullable=False, default="{}"),
    sa.Column("preview", sa.Text(), nullable=False, default=""),
    # applied | queued_for_approval | approved | rejected | refused
    sa.Column("outcome", sa.String(32), nullable=False),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "idempotency_key"),
)

approvals = sa.Table(
    "approvals", metadata,
    sa.Column("approval_id", sa.String(64), primary_key=True),
    _tenant(),
    sa.Column("idempotency_key", sa.String(100), nullable=False),
    sa.Column("turn_id", sa.String(64), nullable=False, default=""),
    sa.Column("thread_id", sa.String(200), nullable=False, default=""),
    sa.Column("helper", sa.String(32), nullable=False, default=""),
    sa.Column("skill", sa.String(64), nullable=False, default=""),
    sa.Column("shop_id", sa.String(64), nullable=False, default=""),
    sa.Column("tool", sa.String(100), nullable=False),
    sa.Column("intent_kind", sa.String(100), nullable=False),
    sa.Column("preview", sa.Text(), nullable=False, default=""),
    sa.Column("payload", sa.Text(), nullable=False, default="{}"),
    # pending | approved | rejected | superseded | withdrawn
    sa.Column("status", sa.String(16), nullable=False),
    # A correction makes a new version (RAI-301): the old pending item points
    # at its replacement and the new one at what it replaced.
    sa.Column("supersedes", sa.String(64), nullable=True),
    sa.Column("superseded_by", sa.String(64), nullable=True),
    sa.Column("decided_by", sa.String(100), nullable=False, default=""),
    sa.Column("note", sa.Text(), nullable=False, default=""),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("created_ts", sa.Float(), nullable=False),
    sa.Column("decided_at", sa.String(40), nullable=True),
    sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_approvals_tenant_key"),
    sa.Index("ix_approvals_tenant_status", "tenant_id", "status"),
)

# A message for a thread whose turn is running (RAI-301). The running loop
# reads it at its next step boundary: absorbed (its turn merges into the
# running one) or released (it runs as its own turn right after). The queued
# turn row is the durable fallback, so a crash never loses the message.
thread_inbox = sa.Table(
    "thread_inbox", metadata,
    _tenant(),
    sa.Column("external_id", sa.String(200), nullable=False),
    sa.Column("thread_id", sa.String(200), nullable=False),
    sa.Column("turn_id", sa.String(64), nullable=False),
    # The turn that was running when the message arrived.
    sa.Column("running_turn_id", sa.String(64), nullable=False),
    sa.Column("source", sa.String(32), nullable=False),
    # waiting | absorbed | released | cancelled_task
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("taken_by", sa.String(64), nullable=False, default=""),
    # The steer labels and dispositions of the message's parts (JSON).
    sa.Column("steer", sa.Text(), nullable=False, default="{}"),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("created_ts", sa.Float(), nullable=False),
    sa.Column("taken_at", sa.String(40), nullable=True),
    sa.PrimaryKeyConstraint("tenant_id", "external_id"),
    sa.Index("ix_thread_inbox_thread", "tenant_id", "thread_id", "status"),
)

credit_requests = sa.Table(
    "credit_requests", metadata,
    sa.Column("request_id", sa.String(64), primary_key=True),
    _tenant(),
    sa.Column("shop_id", sa.String(64), nullable=False),
    # What was asked for, from the intent's own payload (computed in code).
    sa.Column("amount_kobo", sa.BigInteger(), nullable=False, default=0),
    sa.Column("reason", sa.Text(), nullable=False, default=""),
    # pending | approved | rejected | noted | withdrawn
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("source_key", sa.String(100), nullable=False),
    sa.Column("approval_id", sa.String(64), nullable=True),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.UniqueConstraint("tenant_id", "source_key", name="uq_credit_requests_tenant_key"),
)

spend = sa.Table(
    "spend", metadata,
    _tenant(),
    sa.Column("day", sa.String(10), nullable=False),  # Africa/Lagos
    sa.Column("kobo", sa.BigInteger(), nullable=False, default=0),
    sa.PrimaryKeyConstraint("tenant_id", "day"),
)

asked_once = sa.Table(
    "asked_once", metadata,
    _tenant(),
    sa.Column("topic", sa.String(100), nullable=False),
    sa.Column("day", sa.String(10), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "topic", "day"),
)

kill_switches = sa.Table(
    "kill_switches", metadata,
    _tenant(),
    sa.Column("helper", sa.String(32), nullable=False),  # "*" stops the whole tenant
    sa.PrimaryKeyConstraint("tenant_id", "helper"),
)

history = sa.Table(
    "history", metadata,
    _tenant(),
    sa.Column("thread_id", sa.String(200), nullable=False),
    sa.Column("turn_id", sa.String(64), nullable=False),
    sa.Column("ts", sa.Float(), nullable=False),
    sa.Column("body", sa.Text(), nullable=False),  # JSON list of messages as sent
    sa.PrimaryKeyConstraint("tenant_id", "thread_id", "turn_id"),
)

prefixes = sa.Table(
    "prefixes", metadata,
    _tenant(),
    sa.Column("thread_id", sa.String(200), nullable=False),
    sa.Column("helper", sa.String(32), nullable=False),
    sa.Column("body", sa.Text(), nullable=True),  # ThreadPrefix JSON
    sa.Column("window_from", sa.Integer(), nullable=False, default=0),
    sa.PrimaryKeyConstraint("tenant_id", "thread_id", "helper"),
)

artifacts = sa.Table(
    "artifacts", metadata,
    sa.Column("path", sa.String(300), primary_key=True),
    _tenant(),
    sa.Column("content", sa.Text(), nullable=False),
)

# ------------------------------------------------------------- demo mode
# MIMI_MODE=demo (mimi.api.demo): one row per visitor session. The token and
# the visitor's address are kept only as digests. The session's copy of the
# synthetic world lives under ``session_tenant`` (named so a tenant reset
# never deletes the row: the per-address count and cap outlive the copy).

demo_sessions = sa.Table(
    "demo_sessions", metadata,
    sa.Column("token_digest", sa.String(64), primary_key=True),
    sa.Column("session_tenant", sa.String(64), nullable=False),
    sa.Column("ip_hash", sa.String(64), nullable=False),
    sa.Column("day", sa.String(10), nullable=False),  # Africa/Lagos, the day it started
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("created_ts", sa.Float(), nullable=False),
    sa.Column("expires_ts", sa.Float(), nullable=False),
    sa.Column("swept", sa.Boolean(), nullable=False, default=False),
    sa.Index("ix_demo_sessions_ip", "ip_hash", "day"),
    sa.Index("ix_demo_sessions_tenant", "session_tenant"),
)

# Model spend of swept sessions, per Africa/Lagos day and visitor address, so
# the daily caps still count it after the copy is deleted.
demo_usage = sa.Table(
    "demo_usage", metadata,
    sa.Column("day", sa.String(10), nullable=False),
    sa.Column("ip_hash", sa.String(64), nullable=False),
    sa.Column("kobo", sa.BigInteger(), nullable=False, default=0),
    sa.PrimaryKeyConstraint("day", "ip_hash"),
)

BOOK_TABLES = (reps, shops, consent_withheld, skus, deliveries, delivery_lines, receivables,
               payments, bank_feed, promises, disputes)
AGENT_TABLES = (threads, inbound_messages, turns, turn_attempts, messages, thread_events,
                intents, approvals, credit_requests, spend, asked_once, kill_switches, history,
                prefixes, artifacts)
