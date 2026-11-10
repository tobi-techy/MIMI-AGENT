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

metadata: sa.MetaData = sa.MetaData()


def _tenant() -> sa.Column[str]:
    return sa.Column("tenant_id", sa.String(64), nullable=False)


def _proof() -> tuple[sa.Column[str], ...]:
    """Every record carries its proof level (RAI-295, ``mimi.provenance``): the
    level, the witnesses behind it (JSON list of kind and source row id) and
    the rules version that set it. Alembic ``0010_proof_levels``."""
    return (
        sa.Column("proof_level", sa.String(16), nullable=False, default="estimated"),
        sa.Column("witnesses", sa.Text(), nullable=False, default="[]"),
        sa.Column("proof_policy_version", sa.String(16), nullable=False, default="v1"),
    )


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
    *_proof(),
    sa.PrimaryKeyConstraint("tenant_id", "delivery_id"),
    # A shop's last deliveries, newest first (the shop page, the helper's
    # ``deliveries`` read): tenant, shop, then the day, so no sort. Replaced
    # the old (tenant, shop) index in alembic 0019.
    sa.Index("ix_deliveries_tenant_shop_day", "tenant_id", "shop_id", "day", "delivery_id"),
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
    # Batch and expiry from the delivery note (sales tracking, alembic 0015);
    # NULL when the note gave none. Africa/Lagos day.
    sa.Column("expires_on", sa.String(10), nullable=True),
    sa.Column("batch", sa.String(64), nullable=True),
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
    # The full shop history (``SqlGo.today`` reads a shop's invoices whatever
    # their state); the open ones read the partial index below.
    sa.Index("ix_receivables_tenant_shop", "tenant_id", "shop_id"),
    # Every open-invoice read: the credit watch and morning brief aggregate the
    # tenant's open book in position order without a sort, the overdue roll
    # filters due_day, and the returns ledger locks a shop's open invoices in
    # the same order. Paid invoices drop out, so the index shrinks as a book is
    # collected instead of growing with every invoice ever written. Layered on
    # the full (tenant, shop) index above, which a shop's whole history
    # (``SqlGo.today``) still reads. Alembic 0019.
    sa.Index("ix_receivables_tenant_open", "tenant_id", "position", "delivery_id",
             postgresql_where=sa.text("outstanding_kobo > 0"),
             sqlite_where=sa.text("outstanding_kobo > 0")),
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
    *_proof(),
    sa.PrimaryKeyConstraint("tenant_id", "payment_id"),
    # A shop's payments, newest day first (the shop page). Replaced the old
    # (tenant, shop) index in alembic 0019: the day is the sort key, so the
    # index answers the whole read.
    sa.Index("ix_payments_tenant_shop_day", "tenant_id", "shop_id", "day"),
    # The morning brief and the dashboard read the tenant's last days of
    # payments (day >= today - 7), not its whole history. Alembic 0019.
    sa.Index("ix_payments_tenant_day", "tenant_id", "day"),
    # Transfer alerts no money backed are rare; the brief reads only those,
    # oldest first, so the partial index holds the exceptions, not the book.
    sa.Index("ix_payments_tenant_fake_day", "tenant_id", "day",
             postgresql_where=sa.text("fake_alert IS TRUE"),
             sqlite_where=sa.text("fake_alert IS TRUE")),
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
    # Matching an inbound transfer to a payment: the match reads the credits
    # for one amount first, then joins its payment (alembic 0019).
    sa.Index("ix_bank_feed_tenant_amount", "tenant_id", "amount_kobo"),
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
    # A shop's promises on its page (alembic 0019).
    sa.Index("ix_promises_tenant_shop", "tenant_id", "shop_id"),
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
    # One shop's disputes and the tenant's open count (the shop page, the
    # morning brief). Alembic 0019.
    sa.Index("ix_disputes_tenant_shop_status", "tenant_id", "shop_id", "status"),
)

# Every change to a record's proof level, append-only (RAI-295): the witness
# that landed (or "policy" for a rules bump), the old and new level, when.
proof_events = sa.Table(
    "proof_events", metadata,
    _tenant(),
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
    sa.Index("ix_proof_events_record", "tenant_id", "record_type", "record_id"),
    # A record's proof history in the order it landed, without naming the
    # record type first (``SqlProofBook.events``). Alembic 0019.
    sa.Index("ix_proof_events_tenant_record", "tenant_id", "record_id", "at"),
)

# A shop is asked to confirm a record once; a skipped ask is never chased.
proof_asks = sa.Table(
    "proof_asks", metadata,
    _tenant(),
    sa.Column("record_type", sa.String(32), nullable=False),
    sa.Column("record_id", sa.String(64), nullable=False),
    sa.Column("asked_at", sa.String(40), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "record_type", "record_id"),
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
    # The console inbox: one tenant's threads, newest activity first, which is
    # the same order it pages in. Alembic 0019.
    sa.Index("ix_threads_tenant_last", "tenant_id", "last_ts", "thread_id"),
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
    # The queue's candidate read joins inbound_messages on (tenant, turn): the
    # index turns a scan of every inbound message into a lookup per candidate.
    # Alembic 0019.
    sa.Index("ix_inbound_messages_tenant_turn", "tenant_id", "turn_id"),
    # A thread's attachments (demo mode reads its uploads back by thread).
    sa.Index("ix_inbound_messages_tenant_thread", "tenant_id", "thread_id"),
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
    # The console's task and activity lists: one tenant's turns, newest first.
    # It is not a claim index: claiming filters on state and the lease and is
    # served by ix_turns_state above (the queue owns that read). Alembic 0019.
    sa.Index("ix_turns_tenant_created", "tenant_id", "created_ts"),
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
    # An approval's draft message: one row per idempotency key, read once per
    # approval in the list (a tenant-scoped scan without it). Alembic 0019.
    sa.Index("ix_messages_tenant_idem", "tenant_id", "idempotency_key"),
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
    # The Activity view reads one turn's steps without naming its thread;
    # without this it scans every event the tenant ever wrote. Alembic 0019.
    sa.Index("ix_thread_events_tenant_turn", "tenant_id", "turn_id"),
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
    # "" (approve: Mimi carries it out once approved) or "hand_off" (the owner
    # does it by hand; approving only records that it was done). RAI-323.
    sa.Column("handling", sa.String(16), nullable=False, default=""),
    sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_approvals_tenant_key"),
    # The approvals list: one tenant, one status, newest first (the sort key is
    # last, so the read is answered by the index). Replaced the old
    # (tenant, status) index in alembic 0019.
    sa.Index("ix_approvals_tenant_status_created", "tenant_id", "status", "created_ts"),
    # A thread's waiting approvals: the inbox summary counts them per row and
    # the steering reads find the thread's pending items. Alembic 0019.
    sa.Index("ix_approvals_tenant_thread_status", "tenant_id", "thread_id", "status"),
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
    # The context builder and the compactor read a thread's history in time
    # order; the primary key orders by turn id, not time. Alembic 0019.
    sa.Index("ix_history_tenant_thread_ts", "tenant_id", "thread_id", "ts"),
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

# Thread compaction (RAI-285): one row per summary version. Raw history in
# ``history`` is never deleted; each version covers a window of turn ids and
# the builder shows the newest version plus the turns after its window. The
# ``due_ts`` row (version 0, no body) is the durable timer: a thread whose
# big turn passed the threshold compacts once it is due and no turn is queued.
compactions = sa.Table(
    "compactions", metadata,
    _tenant(),
    sa.Column("thread_id", sa.String(200), nullable=False),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("helper", sa.String(32), nullable=False, default=""),
    sa.Column("skill", sa.String(64), nullable=False, default=""),
    # ThreadSummary JSON (version 0: the pending timer, empty).
    sa.Column("body", sa.Text(), nullable=False, default=""),
    sa.Column("summary_hash", sa.String(16), nullable=False, default=""),
    # Turn ids covered, oldest first, JSON list (audit, never shown).
    sa.Column("covered_turns", sa.Text(), nullable=False, default="[]"),
    sa.Column("generation", sa.Integer(), nullable=False, default=0),
    sa.Column("tokens_before", sa.Integer(), nullable=False, default=0),
    sa.Column("tokens_after", sa.Integer(), nullable=False, default=0),
    # Due epoch seconds for the pending timer (version 0 only).
    sa.Column("due_ts", sa.Float(), nullable=False, default=0.0),
    sa.Column("created_at", sa.String(40), nullable=False, default=""),
    sa.PrimaryKeyConstraint("tenant_id", "thread_id", "version"),
    # The compactor polls due timer rows (version 0 only) every tick; the
    # partial index holds one entry per thread waiting, not every summary.
    # Alembic 0019.
    sa.Index("ix_compactions_due", "due_ts",
             postgresql_where=sa.text("version = 0"),
             sqlite_where=sa.text("version = 0")),
)

artifacts = sa.Table(
    "artifacts", metadata,
    sa.Column("path", sa.String(300), primary_key=True),
    _tenant(),
    sa.Column("content", sa.Text(), nullable=False),
)

# When to speak (RAI-300): the owner's digest queue (the daily note, RAI-289,
# reads what it has not consumed) and one row per unprompted message, so the
# per-audience daily cap counts across restarts and workers.
digest_queue = sa.Table(
    "digest_queue", metadata,
    sa.Column("item_id", sa.Integer(), primary_key=True, autoincrement=True),
    _tenant(),
    sa.Column("shop_id", sa.String(64), nullable=False, default=""),
    sa.Column("body", sa.Text(), nullable=False, default=""),
    # The trace id of the turn or routine that queued the item (observability,
    # alembic 0020). Nullable for rows written before it existed.
    sa.Column("trace_id", sa.String(64), nullable=True),
    sa.Column("kind", sa.String(32), nullable=False, default="reply"),
    sa.Column("day", sa.String(10), nullable=False),  # Africa/Lagos
    sa.Column("reason", sa.Text(), nullable=False, default=""),
    sa.Column("consumed", sa.Boolean(), nullable=False, default=False),
    sa.Column("created_at", sa.String(40), nullable=False, default=""),
    sa.Index("ix_digest_queue_tenant_day", "tenant_id", "day", "consumed"),
)

speak_counts = sa.Table(
    "speak_counts", metadata,
    sa.Column("count_id", sa.Integer(), primary_key=True, autoincrement=True),
    _tenant(),
    sa.Column("audience", sa.String(16), nullable=False),  # owner | shop | rep
    sa.Column("shop_id", sa.String(64), nullable=False, default=""),
    sa.Column("day", sa.String(10), nullable=False),  # Africa/Lagos
    sa.Column("created_at", sa.String(40), nullable=False, default=""),
    sa.Index("ix_speak_counts_tenant_day", "tenant_id", "audience", "shop_id", "day"),
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

# ------------------------------------------------------------- connectors (RAI-277)
# connector_accounts holds no tokens: Composio holds them. connector_writes
# is the audit of every outside write (proposal id, idempotency key, the
# external id, the approval, applied and undone times). consent_grants sits
# next to consent_withheld: one row per shop, receiver, scope and grant time.

connector_accounts = sa.Table(
    "connector_accounts", metadata,
    _tenant(),
    sa.Column("connection_id", sa.String(64), nullable=False),
    sa.Column("toolkit", sa.String(32), nullable=False),  # ZOHO_BOOKS | ZOHO | GOOGLESHEETS
    sa.Column("connected_account_id", sa.String(100), nullable=False, default=""),
    sa.Column("region", sa.String(8), nullable=False, default="com"),
    # active | revoked
    sa.Column("status", sa.String(16), nullable=False, default="active"),
    sa.Column("connected_at", sa.String(40), nullable=False),
    sa.Column("revoked_at", sa.String(40), nullable=True),
    sa.PrimaryKeyConstraint("tenant_id", "connection_id"),
)

connector_sync_runs = sa.Table(
    "connector_sync_runs", metadata,
    _tenant(),
    sa.Column("run_id", sa.String(64), nullable=False),
    sa.Column("connection_id", sa.String(64), nullable=False),
    sa.Column("shop_id", sa.String(64), nullable=False, default=""),
    sa.Column("scope", sa.String(32), nullable=False, default="stock"),
    sa.Column("rows_read", sa.Integer(), nullable=False, default=0),
    sa.Column("rows_mapped", sa.Integer(), nullable=False, default=0),
    sa.Column("errors", sa.Text(), nullable=False, default="[]"),
    sa.Column("started_at", sa.String(40), nullable=False),
    sa.Column("finished_at", sa.String(40), nullable=True),
    sa.PrimaryKeyConstraint("tenant_id", "run_id"),
    sa.Index("ix_connector_sync_runs_conn", "tenant_id", "connection_id"),
)

connector_writes = sa.Table(
    "connector_writes", metadata,
    _tenant(),
    sa.Column("write_id", sa.String(64), nullable=False),
    sa.Column("proposal_id", sa.String(64), nullable=False),
    sa.Column("idempotency_key", sa.String(100), nullable=False),
    # The trace id of the run that made the write (observability, alembic 0020).
    # Nullable: writes before 0020 have none, and the column is filled by the
    # writer from the day it ships. Nullable plus an index is the expand half of
    # expand and contract; the NOT NULL comes later, once every writer sets it.
    sa.Column("trace_id", sa.String(64), nullable=True),
    sa.Column("action", sa.String(100), nullable=False),
    sa.Column("payload_hash", sa.String(64), nullable=False, default=""),
    sa.Column("before", sa.Text(), nullable=False, default="{}"),
    sa.Column("after", sa.Text(), nullable=False, default="{}"),
    sa.Column("external_id", sa.String(100), nullable=False, default=""),
    sa.Column("approval_id", sa.String(64), nullable=False, default=""),
    sa.Column("applied_at", sa.String(40), nullable=False),
    sa.Column("undone_at", sa.String(40), nullable=True),
    sa.PrimaryKeyConstraint("tenant_id", "write_id"),
    sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_connector_writes_tenant_key"),
    # A write is found by its trace when no approval is behind it (alembic 0020).
    sa.Index("ix_connector_writes_tenant_trace", "tenant_id", "trace_id"),
)

consent_grants = sa.Table(
    "consent_grants", metadata,
    _tenant(),
    sa.Column("shop_id", sa.String(64), nullable=False),
    sa.Column("receiver", sa.String(64), nullable=False),
    sa.Column("scope", sa.String(32), nullable=False),
    sa.Column("granted_at", sa.String(40), nullable=False),
    sa.Column("revoked_at", sa.String(40), nullable=True),
    sa.PrimaryKeyConstraint("tenant_id", "shop_id", "receiver", "scope", "granted_at"),
    sa.Index("ix_consent_grants_shop", "tenant_id", "shop_id", "scope"),
)

# Connector rows read into the book (RAI-277): one row per shop, item and
# count day, with the source that produced it (connector | checkin).
stock_counts = sa.Table(
    "stock_counts", metadata,
    _tenant(),
    sa.Column("shop_id", sa.String(64), nullable=False),
    sa.Column("sku", sa.String(64), nullable=False),
    sa.Column("item", sa.String(200), nullable=False, default=""),
    sa.Column("quantity", sa.Integer(), nullable=False),
    sa.Column("counted_on", sa.String(10), nullable=False),  # Africa/Lagos day
    sa.Column("source", sa.String(16), nullable=False, default="connector"),
    sa.PrimaryKeyConstraint("tenant_id", "shop_id", "sku", "counted_on", "source"),
)

# The distributor's rulebook (RAI-323, alembic 0014): one behaviour per action
# type the owner changed; no row means the default. Built-in stops never have
# a row. Only the API (owner key) writes here; nothing a helper or a message
# says can.
policy_rules = sa.Table(
    "policy_rules", metadata,
    _tenant(),
    sa.Column("action_type", sa.String(64), nullable=False),
    sa.Column("behaviour", sa.String(24), nullable=False),
    sa.Column("updated_by", sa.String(100), nullable=False, default=""),
    sa.Column("updated_at", sa.String(40), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "action_type"),
)

# Every rule change: who, when, from what to what.
policy_changes = sa.Table(
    "policy_changes", metadata,
    sa.Column("change_id", sa.String(64), primary_key=True),
    _tenant(),
    sa.Column("action_type", sa.String(64), nullable=False),
    sa.Column("old_behaviour", sa.String(24), nullable=False),
    sa.Column("new_behaviour", sa.String(24), nullable=False),
    sa.Column("changed_by", sa.String(100), nullable=False),
    sa.Column("key_digest", sa.String(64), nullable=False, default=""),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("created_ts", sa.Float(), nullable=False),
    sa.Index("ix_policy_changes_tenant", "tenant_id", "created_ts"),
)

# Weekly check-in state (RAI-276), durable (sales tracking, alembic 0015):
# one row per shop and ISO week (idempotent on the scheduler's event id), and
# one row per shop for its consecutive skips, answered streak, Connect offer
# and stop. Before this it lived in process memory, so MIMI_CHECKIN could not
# safely be on with more than one worker or across a restart.
checkin_weeks = sa.Table(
    "checkin_weeks", metadata,
    _tenant(),
    sa.Column("shop_id", sa.String(64), nullable=False),
    sa.Column("week", sa.String(8), nullable=False),
    sa.Column("event_id", sa.String(200), nullable=False),
    # asked | answered | skipped | stopped | connected_offered
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("low_stock_sku", sa.String(64), nullable=False, default=""),
    sa.Column("answered_at", sa.String(40), nullable=False, default=""),
    sa.Column("updated_at", sa.String(40), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "shop_id", "week"),
    sa.Index("ix_checkin_weeks_event", "tenant_id", "event_id"),
)

checkin_shops = sa.Table(
    "checkin_shops", metadata,
    _tenant(),
    sa.Column("shop_id", sa.String(64), nullable=False),
    sa.Column("skips", sa.Integer(), nullable=False, default=0),
    sa.Column("streak", sa.Integer(), nullable=False, default=0),
    sa.Column("connect_offered", sa.Boolean(), nullable=False, default=False),
    sa.Column("updated_at", sa.String(40), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "shop_id"),
)

# Computer tasks (RAI-326): one task, its steps and its artifacts. A task is a
# goal, an approved domain and a code-built plan the owner approves once. No
# credential, no card number and no page text is stored: a step keeps a redacted
# note and the name of a screenshot artifact.
computer_tasks = sa.Table(
    "computer_tasks", metadata,
    _tenant(),
    sa.Column("task_id", sa.String(64), nullable=False),
    sa.Column("goal", sa.Text(), nullable=False),
    sa.Column("url", sa.String(2000), nullable=False, default=""),
    sa.Column("domain", sa.String(255), nullable=False, default=""),
    sa.Column("kind", sa.String(32), nullable=False, default="generic"),
    sa.Column("status", sa.String(24), nullable=False, default="proposed"),
    sa.Column("plan", sa.Text(), nullable=False, default="[]"),  # JSON list of steps
    sa.Column("inputs", sa.Text(), nullable=False, default="{}"),  # JSON: owner values
    sa.Column("limits", sa.Text(), nullable=False, default="{}"),  # JSON GuardLimits
    sa.Column("engine", sa.String(40), nullable=False, default=""),
    sa.Column("stop_reason", sa.String(32), nullable=False, default=""),
    sa.Column("note", sa.Text(), nullable=False, default=""),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("created_ts", sa.Float(), nullable=False),
    sa.Column("started_at", sa.String(40), nullable=False, default=""),
    sa.Column("finished_at", sa.String(40), nullable=False, default=""),
    sa.PrimaryKeyConstraint("tenant_id", "task_id"),
    sa.Index("ix_computer_tasks_created", "tenant_id", "created_ts"),
)

computer_steps = sa.Table(
    "computer_steps", metadata,
    _tenant(),
    sa.Column("task_id", sa.String(64), nullable=False),
    sa.Column("idx", sa.Integer(), nullable=False),
    sa.Column("action", sa.String(32), nullable=False),
    sa.Column("url", sa.String(2000), nullable=False, default=""),
    sa.Column("note", sa.Text(), nullable=False, default=""),
    sa.Column("screenshot", sa.String(120), nullable=False, default=""),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "task_id", "idx"),
)

computer_artifacts = sa.Table(
    "computer_artifacts", metadata,
    _tenant(),
    sa.Column("task_id", sa.String(64), nullable=False),
    sa.Column("name", sa.String(120), nullable=False),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("mime", sa.String(64), nullable=False, default=""),
    sa.Column("size", sa.Integer(), nullable=False, default=0),
    sa.Column("sha256", sa.String(64), nullable=False, default=""),
    sa.Column("path", sa.String(500), nullable=False, default=""),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "task_id", "name"),
)

# ------------------------------------------------------------- returns
# The returns ledger (feat/returns-and-exposure): a return opens from an
# approved slow-stock pickup option, then drafted, approved, collected,
# inspected, settled (or cancelled). Quantities and naira are computed in
# code; the rep collects, because Mimi never moves stock. A settlement is a
# ``return_credit`` approval the owner approves, applied to the shop's open
# invoices, undoable (``credit_notes``). Alembic ``0017_returns_exposure``.

returns = sa.Table(
    "returns", metadata,
    _tenant(),
    sa.Column("return_id", sa.String(64), nullable=False),
    sa.Column("shop_id", sa.String(64), nullable=False),
    # slow_stock (opened from an approved pickup) | manual
    sa.Column("source", sa.String(16), nullable=False, default="manual"),
    sa.Column("approval_id", sa.String(64), nullable=True),
    sa.Column("reason", sa.Text(), nullable=False, default=""),
    # drafted | approved | collected | inspected | settled | cancelled
    sa.Column("status", sa.String(16), nullable=False, default="drafted"),
    sa.Column("value_drafted_kobo", sa.BigInteger(), nullable=False, default=0),
    sa.Column("value_collected_kobo", sa.BigInteger(), nullable=False, default=0),
    sa.Column("value_returned_kobo", sa.BigInteger(), nullable=False, default=0),
    # Value of what came back able to sell again or go to the supplier.
    sa.Column("recovered_kobo", sa.BigInteger(), nullable=False, default=0),
    # The owner's configured pickup cost, and what the return saved or cost.
    sa.Column("pickup_cost_kobo", sa.BigInteger(), nullable=True),
    sa.Column("saving_kobo", sa.BigInteger(), nullable=True),
    sa.Column("credit_kobo", sa.BigInteger(), nullable=True),
    sa.Column("credit_approval_id", sa.String(64), nullable=True),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("created_ts", sa.Float(), nullable=False),
    sa.Column("updated_at", sa.String(40), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "return_id"),
    sa.Index("ix_returns_tenant_status", "tenant_id", "status"),
    sa.Index("ix_returns_tenant_shop", "tenant_id", "shop_id"),
)

return_lines = sa.Table(
    "return_lines", metadata,
    _tenant(),
    sa.Column("return_id", sa.String(64), nullable=False),
    sa.Column("line_no", sa.Integer(), nullable=False),
    sa.Column("sku", sa.String(64), nullable=False),
    sa.Column("item", sa.String(200), nullable=False, default=""),
    sa.Column("unit", sa.String(32), nullable=False, default=""),
    sa.Column("reason", sa.Text(), nullable=False, default=""),
    sa.Column("quantity_drafted", sa.Integer(), nullable=False, default=0),
    sa.Column("quantity_collected", sa.Integer(), nullable=True),
    sa.Column("quantity_returned", sa.Integer(), nullable=True),
    sa.Column("unit_price_kobo", sa.BigInteger(), nullable=False, default=0),
    sa.Column("value_drafted_kobo", sa.BigInteger(), nullable=False, default=0),
    sa.Column("value_returned_kobo", sa.BigInteger(), nullable=True),
    # resellable | damaged | expired | "" (before inspection)
    sa.Column("condition", sa.String(16), nullable=False, default=""),
    # resell | write_off | return_to_supplier | "" (before inspection)
    sa.Column("disposition", sa.String(24), nullable=False, default=""),
    # Batch and expiry from the distributor's own invoice line (alembic 0015)
    # when the note carried them; ``expiry_source`` says which.
    sa.Column("expires_on", sa.String(10), nullable=True),
    sa.Column("batch", sa.String(64), nullable=False, default=""),
    sa.Column("expiry_source", sa.String(16), nullable=False, default="estimated"),
    sa.PrimaryKeyConstraint("tenant_id", "return_id", "line_no"),
)

return_events = sa.Table(
    "return_events", metadata,
    _tenant(),
    sa.Column("event_id", sa.String(64), nullable=False),
    sa.Column("return_id", sa.String(64), nullable=False),
    sa.Column("seq", sa.Integer(), nullable=False, default=0),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("by", sa.String(100), nullable=False, default=""),
    sa.Column("note", sa.Text(), nullable=False, default=""),
    sa.Column("at", sa.String(40), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "event_id"),
    sa.Index("ix_return_events_return", "tenant_id", "return_id", "seq"),
)

# The settlement credits actually applied: what came off the shop's invoices
# (``applications``, oldest first) and what is held on the shop's account
# because it owed nothing. Undo reverses both. One row per settle attempt.
credit_notes = sa.Table(
    "credit_notes", metadata,
    _tenant(),
    sa.Column("credit_id", sa.String(64), nullable=False),
    sa.Column("return_id", sa.String(64), nullable=False),
    sa.Column("shop_id", sa.String(64), nullable=False),
    sa.Column("approval_id", sa.String(64), nullable=False, default=""),
    sa.Column("amount_kobo", sa.BigInteger(), nullable=False),
    sa.Column("applied_kobo", sa.BigInteger(), nullable=False, default=0),
    sa.Column("on_account_kobo", sa.BigInteger(), nullable=False, default=0),
    sa.Column("applications", sa.Text(), nullable=False, default="[]"),
    sa.Column("applied_at", sa.String(40), nullable=False, default=""),
    sa.Column("undone_at", sa.String(40), nullable=True),
    sa.PrimaryKeyConstraint("tenant_id", "credit_id"),
    sa.Index("ix_credit_notes_return", "tenant_id", "return_id"),
)

# The owner's configured pickup cost (what a collection trip costs him), so a
# return can report what the pickup saved or cost. No row: not configured.
returns_settings = sa.Table(
    "returns_settings", metadata,
    sa.Column("tenant_id", sa.String(64), primary_key=True),
    sa.Column("pickup_cost_kobo", sa.BigInteger(), nullable=True),
    sa.Column("updated_at", sa.String(40), nullable=False),
)

# ------------------------------------------------------------- routines
# Scheduled routines (4 Nov 2026, ``mimi.api.routines``): a routine files its
# findings on the owner's thread on a schedule in Africa/Lagos, without anyone
# asking. ``routine_state`` holds one row per routine and tenant, plus the
# kill switch row (routine ``*``: one row pauses every routine without a
# deploy); it survives restarts. ``routine_runs`` claims one day per routine
# with its primary key, so a restart or a second API process can never fire
# the same routine twice on the same Lagos day. Alembic ``0018_routines``.

routine_state = sa.Table(
    "routine_state", metadata,
    _tenant(),
    sa.Column("routine", sa.String(64), nullable=False),  # the id, or "*" for all
    sa.Column("paused", sa.Boolean(), nullable=False, default=False),
    sa.Column("pause_reason", sa.Text(), nullable=False, default=""),
    sa.Column("paused_at", sa.String(40), nullable=False, default=""),
    sa.Column("last_run_day", sa.String(10), nullable=False, default=""),  # Africa/Lagos
    sa.Column("last_run_at", sa.String(40), nullable=False, default=""),
    # done | failed | skipped | missed (the read says it in words)
    sa.Column("last_status", sa.String(16), nullable=False, default=""),
    sa.Column("last_finding", sa.Text(), nullable=False, default=""),
    # The shop the last finding is about, when it is about exactly one shop;
    # "" when it is about several or the whole book (the console then shows
    # the sentence as plain text, never a guessed link).
    sa.Column("last_finding_shop_id", sa.String(64), nullable=False, default=""),
    sa.Column("last_error", sa.Text(), nullable=False, default=""),
    sa.Column("last_skip_day", sa.String(10), nullable=False, default=""),
    sa.Column("last_missed_day", sa.String(10), nullable=False, default=""),
    sa.Column("updated_at", sa.String(40), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "routine"),
)

routine_runs = sa.Table(
    "routine_runs", metadata,
    _tenant(),
    sa.Column("routine", sa.String(64), nullable=False),
    sa.Column("day", sa.String(10), nullable=False),  # Africa/Lagos: the claim key
    # running | done | failed
    sa.Column("status", sa.String(16), nullable=False),
    # Which API process ran it (host, pid, thread): diagnostics only.
    sa.Column("owner", sa.String(160), nullable=False, default=""),
    sa.Column("started_at", sa.String(40), nullable=False),
    sa.Column("started_ts", sa.Float(), nullable=False),
    sa.Column("finished_at", sa.String(40), nullable=False, default=""),
    sa.Column("findings", sa.Integer(), nullable=False, default=0),
    sa.Column("result", sa.Text(), nullable=False, default=""),
    sa.Column("error", sa.Text(), nullable=False, default=""),
    sa.PrimaryKeyConstraint("tenant_id", "routine", "day"),
)

BOOK_TABLES = (reps, shops, consent_withheld, skus, deliveries, delivery_lines, receivables,
               payments, bank_feed, promises, disputes, proof_events, proof_asks)
AGENT_TABLES = (threads, inbound_messages, turns, turn_attempts, messages, thread_events,
                intents, approvals, credit_requests, spend, asked_once, kill_switches, history,
                prefixes, artifacts, compactions, computer_tasks, computer_steps,
                computer_artifacts, returns, return_lines, return_events, credit_notes,
                returns_settings, routine_state, routine_runs)

# Identity (Phase 1 slice 1): the distributor's people and the areas they
# cover. Imported last, for its side effect, so every module keeps defining
# its tables on this one ``metadata`` (alembic ``0021_identity``).
from mimi.store.schema_identity import IDENTITY_TABLES, user_territories, users  # noqa: E402,F401
