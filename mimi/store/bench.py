"""Synthetic pilot-scale data and statement measurement for the store.

Nothing in the app imports this module. It exists so the index audit, the
query budget and the retention job can be measured against a database shaped
like a pilot rather than a demo: ten distributors, ten thousand shops and the
append-only rows a few months of traffic leaves behind.

``python -m mimi.store.bench --url postgresql://... --seed`` fills a scratch
database; ``tests/test_schema_performance.py`` uses the same seeder at a tiny
scale so a regression in a hot query fails CI.

Determinism matters: the same ``Scale`` always writes the same rows, with
synthetic names and phone numbers only (``+234000000xxxx`` ranges that no real
operator assigns to a person in this repo).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.store import schema as s

#: The recording day the offline evals pin (28 Oct 2026, Africa/Lagos).
DAY0 = datetime(2026, 10, 28, 8, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Scale:
    """How much synthetic traffic to write.

    ``pilot`` is the measured scale in docs/DATA.md: 10 distributors, 10,000
    shops, 80,000 turns and 640,000 thread events. ``tiny`` keeps the CI test
    under a second.
    """

    tenants: int = 10
    shops_per_tenant: int = 1000
    reps_per_tenant: int = 40
    skus_per_tenant: int = 40
    deliveries_per_shop: int = 5
    payments_per_shop: int = 5
    turns_per_shop: int = 8
    events_per_turn: int = 8
    messages_per_turn: int = 2
    approvals_per_shop: int = 3
    queued_per_tenant: int = 20


PILOT = Scale()
TINY = Scale(tenants=2, shops_per_tenant=20, reps_per_tenant=4, skus_per_tenant=8,
             deliveries_per_shop=2, payments_per_shop=2, turns_per_shop=3,
             events_per_turn=4, messages_per_turn=2, approvals_per_shop=2,
             queued_per_tenant=3)


@dataclass
class BenchData:
    """Ids the audit reads, so a plan runs against a busy tenant and shop."""

    tenant: str = ""
    shop_id: str = ""
    delivery_id: str = ""
    thread_id: str = ""
    turn_id: str = ""
    owner_thread: str = ""
    approval_key: str = ""
    return_id: str = ""
    tenants: list[str] = field(default_factory=list)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _phone(n: int) -> str:
    # A synthetic block: never a real subscriber number (synthetic data rule).
    return f"+234000{n:07d}"


def _batches(rows: list[dict[str, Any]], size: int = 2000) -> list[list[dict[str, Any]]]:
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def _write(conn: sa.Connection, table: sa.Table, rows: list[dict[str, Any]]) -> None:
    for batch in _batches(rows):
        conn.execute(table.insert(), batch)


def seed(engine: Engine, scale: Scale = PILOT) -> BenchData:
    """Write the synthetic pilot into an empty store. Returns the sample ids."""
    data = BenchData(tenant="t00")
    with engine.begin() as conn:
        for ti in range(scale.tenants):
            tenant = f"t{ti:02d}"
            data.tenants.append(tenant)
            shops = [f"s{ti:02d}-{i:05d}" for i in range(scale.shops_per_tenant)]
            reps = [f"r{ti:02d}-{i:03d}" for i in range(scale.reps_per_tenant)]
            skus = [f"sku{ti:02d}-{i:03d}" for i in range(scale.skus_per_tenant)]
            owner_thread = f"{tenant}:owner"
            if ti == 0:
                data.shop_id = shops[0]
                data.thread_id = f"{tenant}:shop:{shops[0]}"
                data.owner_thread = owner_thread
                data.delivery_id = f"d{ti:02d}-00000-0"

            _write(conn, s.tenants, [{
                "tenant_id": tenant, "name": f"Distributor {ti:02d}",
                "owner_phones": '["+2340000099999"]', "owner_thread": owner_thread,
                "live_date": "2026-11-10", "live_day": 400, "price_rise_day": 360,
                "daily_cap_kobo": 50_000, "collection_account": '{"bank": "synthetic"}',
                "simulated": True, "created_at": _iso(DAY0 - timedelta(days=120)),
            }])
            _write(conn, s.reps, [{
                "tenant_id": tenant, "rep_id": rep, "name": f"Rep {rep}",
                "phone": _phone(ti * 1000 + i + 1),
                "areas": '["Area A"]',
            } for i, rep in enumerate(reps)])
            _write(conn, s.shops, [{
                "tenant_id": tenant, "shop_id": shop, "name": f"Shop {shop}",
                "shop_type": "retail", "size": "small", "owner": f"Owner {shop}",
                "area": "Area A", "street": f"{i} Synthetic Road",
                "phone": _phone(100_000 + ti * 100_000 + i),
                "rep_id": reps[i % len(reps)], "credit_limit_kobo": 500_000,
                "bank": "Synthetic Bank", "restock_days": 7, "simulated": True,
            } for i, shop in enumerate(shops)])
            _write(conn, s.skus, [{
                "tenant_id": tenant, "sku": sku, "name": f"Product {sku}",
                "brand": "Synthetic", "unit": "carton",
                "price_kobo": 120_000 + i * 500, "risen_price_kobo": None,
            } for i, sku in enumerate(skus)])

            # The book: deliveries with lines, invoices, payments and the bank
            # feed that backs some of them.
            deliveries: list[dict[str, Any]] = []
            lines: list[dict[str, Any]] = []
            receivables: list[dict[str, Any]] = []
            payments: list[dict[str, Any]] = []
            feed: list[dict[str, Any]] = []
            promises: list[dict[str, Any]] = []
            disputes: list[dict[str, Any]] = []
            proof_events: list[dict[str, Any]] = []
            for i, shop in enumerate(shops):
                for k in range(scale.deliveries_per_shop):
                    did = f"d{ti:02d}-{i:05d}-{k}"
                    day = 380 + k * 4
                    total = 3 * (120_000 + (i % len(skus)) * 500)
                    deliveries.append({
                        "tenant_id": tenant, "delivery_id": did, "shop_id": shop,
                        "rep_id": reps[i % len(reps)], "day": day,
                        "delivered_on": (DAY0 + timedelta(days=day - 400)).date().isoformat(),
                        "minute": 60 + k, "total_kobo": total,
                        "status": "delivered", "witnessed_at": _iso(DAY0),
                        "live": False, "proof_level": "likely", "witnesses": "[]",
                        "proof_policy_version": "v1",
                    })
                    for n in range(3):
                        lines.append({
                            "tenant_id": tenant, "delivery_id": did, "line_no": n + 1,
                            "sku": skus[(i + n) % len(skus)], "name": f"Product {n}",
                            "unit": "carton", "quantity": 1,
                            "unit_price_kobo": 120_000 + i % len(skus) * 500,
                            "line_total_kobo": 120_000 + i % len(skus) * 500,
                            "expires_on": "2027-01-31", "batch": f"b{ti:02d}{i:05d}{k}",
                        })
                    receivables.append({
                        "tenant_id": tenant, "delivery_id": did, "shop_id": shop,
                        "due_day": day + 14,
                        "due_on": (DAY0 + timedelta(days=day + 14 - 400)).date().isoformat(),
                        "total_kobo": total,
                        "paid_kobo": 0 if k % 2 else total,
                        "outstanding_kobo": total if k % 2 else 0,
                        "status": "open" if k % 2 else "paid",
                        "position": i * 10 + k,
                    })
                for k in range(scale.payments_per_shop):
                    pid = f"p{ti:02d}-{i:05d}-{k}"
                    fake = k == 0 and i % 17 == 0
                    payments.append({
                        "tenant_id": tenant, "payment_id": pid, "shop_id": shop,
                        "delivery_id": f"d{ti:02d}-{i:05d}-{k}",
                        "day": 390 + k,
                        "paid_on": (DAY0 + timedelta(days=390 + k - 400)).date().isoformat(),
                        "amount_kobo": 120_000, "method": "transfer",
                        "reference": f"ref{ti:02d}{i:05d}{k}", "status": "confirmed",
                        "partial": False, "split_of": None, "fake_alert": fake,
                        "proof_level": "verified" if not fake else "estimated",
                        "witnesses": "[]", "proof_policy_version": "v1",
                    })
                    if not fake and k % 2 == 0:
                        feed.append({
                            "tenant_id": tenant, "credit_id": f"c{ti:02d}-{i:05d}-{k}",
                            "payment_id": pid, "amount_kobo": 120_000,
                            "received_at": _iso(DAY0 + timedelta(days=k)),
                            "narration": f"NIP {shop}",
                        })
                promises.append({
                    "tenant_id": tenant, "promise_id": f"pr{ti:02d}-{i:05d}",
                    "shop_id": shop, "delivery_id": f"d{ti:02d}-{i:05d}-0",
                    "made_on": "2026-11-01", "promised_on": "2026-11-08",
                    "kept": bool(i % 3), "text": "Synthetic promise",
                })
                if i % 2 == 0:
                    did = f"d{ti:02d}-{i:05d}-1"
                    disputes.append({
                        "tenant_id": tenant, "dispute_id": f"dp{ti:02d}-{i:05d}",
                        "shop_id": shop, "delivery_id": did, "day": 390,
                        "sku": skus[i % len(skus)], "missing": 1,
                        "text": "One carton short", "status": "open" if i % 4 else "closed",
                    })
                    proof_events.append({
                        "tenant_id": tenant, "event_id": f"pe{ti:02d}-{i:05d}",
                        "record_type": "delivery", "record_id": did,
                        "old_level": "estimated", "new_level": "likely",
                        "witness_kind": "rep", "witness_source": "synthetic",
                        "policy_version": "v1", "at": _iso(DAY0),
                    })
            _write(conn, s.deliveries, deliveries)
            _write(conn, s.delivery_lines, lines)
            _write(conn, s.receivables, receivables)
            _write(conn, s.payments, payments)
            _write(conn, s.bank_feed, feed)
            _write(conn, s.promises, promises)
            _write(conn, s.disputes, disputes)
            _write(conn, s.proof_events, proof_events)

            # The agent: threads, turns, inbound messages, attempts, chat
            # messages, the step ledger and the compaction history.
            threads: list[dict[str, Any]] = [{
                "tenant_id": tenant, "thread_id": owner_thread, "kind": "owner",
                "shop_id": "", "state": "{}", "summary": "", "claim_seq": 0,
                "last_at": _iso(DAY0 + timedelta(days=13)),
                "last_ts": (DAY0 + timedelta(days=13)).timestamp(),
                "created_at": _iso(DAY0 - timedelta(days=30)),
            }]
            turns: list[dict[str, Any]] = []
            inbound: list[dict[str, Any]] = []
            attempts: list[dict[str, Any]] = []
            messages: list[dict[str, Any]] = []
            events: list[dict[str, Any]] = []
            history: list[dict[str, Any]] = []
            approvals: list[dict[str, Any]] = []
            intents: list[dict[str, Any]] = []
            for i, shop in enumerate(shops):
                thread_id = f"{tenant}:shop:{shop}"
                last_ts = (DAY0 + timedelta(days=13, seconds=-i)).timestamp()
                threads.append({
                    "tenant_id": tenant, "thread_id": thread_id, "kind": "shop",
                    "shop_id": shop, "state": "{}", "summary": "", "claim_seq": 0,
                    "last_at": _iso(DAY0 + timedelta(days=13, seconds=-i)),
                    "last_ts": last_ts,
                    "created_at": _iso(DAY0 - timedelta(days=60)),
                })
                for k in range(scale.turns_per_shop):
                    tid = f"trn-{tenant}-{i:05d}-{k}"
                    created = DAY0 + timedelta(days=k * 10, seconds=i % 3600)
                    done = not (i % 97 == 0 and k == scale.turns_per_shop - 1)
                    if ti == 0 and i == 0 and k == 0:
                        data.turn_id = tid
                    turns.append({
                        "turn_id": tid, "tenant_id": tenant, "thread_id": thread_id,
                        "state": "done" if done else "queued", "merged_into": None,
                        "lease_owner": None, "lease_expires_ts": None if not done else 0.0,
                        "version": 2, "attempts": 1, "completions": 1 if done else 0,
                        "batch": "[]", "checkpoint": "{}",
                        "status": "done" if done else "", "helper": "collector",
                        "skill": "order", "run_id": f"run-{tid}", "result": "{}",
                        "error": "", "stop": False, "simulated": False, "solo": False,
                        "created_at": _iso(created), "created_ts": created.timestamp(),
                        "finished_at": _iso(created + timedelta(minutes=2)) if done else None,
                    })
                    inbound.append({
                        "tenant_id": tenant, "external_id": f"wamid-{tenant}-{i:05d}-{k}",
                        "turn_id": tid, "thread_id": thread_id, "source": "shop_whatsapp",
                        "envelope": '{"text": "Synthetic inbound"}', "media_ref": "",
                        "simulated": False, "received_at": _iso(created),
                    })
                    attempts.append({
                        "turn_id": tid, "worker_id": f"worker-{i % 4}", "takeover": False,
                        "claimed_at": _iso(created),
                    })
                    for m in range(scale.messages_per_turn):
                        mid = f"m{tenant}-{i:05d}-{k}-{m}"
                        messages.append({
                            "tenant_id": tenant, "message_id": mid,
                            "thread_id": thread_id, "turn_id": tid,
                            "direction": "in" if m % 2 == 0 else "out",
                            "sender": "shop" if m % 2 == 0 else "mimi",
                            "channel": "shop_whatsapp",
                            "shop_id": shop if m % 2 == 0 else "",
                            "text": f"Synthetic message {i}-{k}-{m}",
                            "kind": "", "status": "sent",
                            "idempotency_key": "", "simulated": False,
                            "at": _iso(created + timedelta(seconds=m)),
                            "ts": (created + timedelta(seconds=m)).timestamp(),
                        })
                    for e in range(scale.events_per_turn):
                        events.append({
                            "tenant_id": tenant, "event_id": f"ev-{tenant}-{i:05d}-{k}-{e}",
                            "thread_id": thread_id, "run_id": f"run-{tid}", "turn_id": tid,
                            "seq": e, "step": e, "type": "step_finished",
                            "tool": "read_shop", "tier": "read", "short": "Read the shop",
                            "cost_kobo": 10, "model": "synthetic",
                            "duration_ms": 12.0, "data": "{}",
                            "at": _iso(created + timedelta(seconds=e)),
                            "ts": (created + timedelta(seconds=e)).timestamp(),
                        })
                    history.append({
                        "tenant_id": tenant, "thread_id": thread_id, "turn_id": tid,
                        "ts": created.timestamp(), "body": "[]",
                    })
                for a in range(scale.approvals_per_shop):
                    key = f"key-{tenant}-{i:05d}-{a}"
                    aid = f"apr-{tenant}-{i:05d}-{a}"
                    status = "pending" if a == 0 and i % 10 == 0 else "approved"
                    created = DAY0 + timedelta(days=10, seconds=i % 3600)
                    approvals.append({
                        "approval_id": aid, "tenant_id": tenant,
                        "idempotency_key": key, "turn_id": f"trn-{tenant}-{i:05d}-0",
                        "thread_id": thread_id, "helper": "collector", "skill": "order",
                        "shop_id": shop, "tool": "draft_restock", "intent_kind": "restock",
                        "preview": "Synthetic preview", "payload": "{}", "status": status,
                        "supersedes": None, "superseded_by": None, "decided_by": "",
                        "note": "", "created_at": _iso(created),
                        "created_ts": created.timestamp(), "decided_at": None,
                        "handling": "",
                    })
                    if ti == 0 and i == 0 and a == 0:
                        data.approval_key = key
                    intents.append({
                        "tenant_id": tenant, "idempotency_key": key, "attempt_key": "",
                        "turn_id": f"trn-{tenant}-{i:05d}-0", "thread_id": thread_id,
                        "tool": "draft_restock", "intent_kind": "restock",
                        "tier": "approve", "shop_id": shop, "payload": "{}",
                        "preview": "Synthetic preview",
                        "outcome": "queued_for_approval",
                        "created_at": _iso(created),
                    })
                    # The approval list joins the draft message by key: seed it
                    # so the plan reads the same table a real approval does.
                    if a == 0:
                        messages.append({
                            "tenant_id": tenant, "message_id": f"draft-{tenant}-{i:05d}",
                            "thread_id": thread_id, "turn_id": f"trn-{tenant}-{i:05d}-0",
                            "direction": "out", "sender": "mimi",
                            "channel": "shop_whatsapp", "shop_id": shop,
                            "text": "Synthetic draft", "kind": "draft",
                            "status": "pending_approval", "idempotency_key": key,
                            "simulated": False, "at": _iso(created),
                            "ts": created.timestamp(),
                        })
            # Turns the scheduler will actually find: queued now or a lapsed lease.
            now = (DAY0 + timedelta(days=14)).timestamp()
            for q in range(scale.queued_per_tenant):
                tid = f"trn-{tenant}-queued-{q:03d}"
                turns.append({
                    "turn_id": tid, "tenant_id": tenant,
                    "thread_id": f"{tenant}:shop:{shops[q]}", "state": "queued",
                    "merged_into": None, "lease_owner": None, "lease_expires_ts": None,
                    "version": 0, "attempts": 0, "completions": 0, "batch": "[]",
                    "checkpoint": "{}", "status": "", "helper": "", "skill": "",
                    "run_id": "", "result": "{}", "error": "", "stop": False,
                    "simulated": False, "solo": False,
                    "created_at": _iso(datetime.fromtimestamp(now, UTC)),
                    "created_ts": now, "finished_at": None,
                })
                inbound.append({
                    "tenant_id": tenant, "external_id": f"wamid-{tenant}-q-{q:03d}",
                    "turn_id": tid, "thread_id": f"{tenant}:shop:{shops[q]}",
                    "source": "shop_whatsapp",
                    "envelope": '{"text": "Synthetic inbound"}', "media_ref": "",
                    "simulated": False,
                    "received_at": _iso(datetime.fromtimestamp(now, UTC)),
                })

            # Unconsumed digest items and the per-audience daily counters.
            digest: list[dict[str, Any]] = []
            for i, shop in enumerate(shops):
                if i % 5:
                    continue
                digest.append({
                    "tenant_id": tenant, "shop_id": shop,
                    "body": f"Synthetic digest {i}", "kind": "reply", "day": "2026-11-10",
                    "reason": "unprompted", "consumed": False,
                    "created_at": _iso(DAY0 + timedelta(days=13)),
                })
            speak = [{
                "tenant_id": tenant, "audience": "shop", "shop_id": shop,
                "day": "2026-11-10", "created_at": _iso(DAY0 + timedelta(days=13)),
            } for i, shop in enumerate(shops) if i % 4 == 0]

            # Compaction timers: a pending version-0 row per thread and a
            # finished version-1 summary, so the compactor's due-timer poll
            # has a real partial index to read instead of an empty table.
            compactions: list[dict[str, Any]] = []
            for i, shop in enumerate(shops):
                if i >= 200:
                    break
                thread_id = f"{tenant}:shop:{shop}"
                for version, due in ((0, now - 60.0 + i), (1, 0.0)):
                    compactions.append({
                        "tenant_id": tenant, "thread_id": thread_id,
                        "version": version, "helper": "", "skill": "",
                        "body": "{}", "summary_hash": "", "covered_turns": "[]",
                        "generation": version, "tokens_before": 0,
                        "tokens_after": 0, "due_ts": due,
                        "created_at": _iso(DAY0 + timedelta(days=13)),
                    })

            _write(conn, s.threads, threads)
            _write(conn, s.turns, turns)
            _write(conn, s.inbound_messages, inbound)
            _write(conn, s.turn_attempts, attempts)
            _write(conn, s.messages, messages)
            _write(conn, s.thread_events, events)
            _write(conn, s.history, history)
            _write(conn, s.approvals, approvals)
            _write(conn, s.intents, intents)
            _write(conn, s.digest_queue, digest)
            _write(conn, s.speak_counts, speak)
            _write(conn, s.compactions, compactions)

            # Routines: the scheduler reads state per tenant every tick.
            _write(conn, s.routine_state, [{
                "tenant_id": tenant, "routine": "morning_brief", "paused": False,
                "pause_reason": "", "paused_at": "", "last_run_day": "2026-11-09",
                "last_run_at": _iso(DAY0 + timedelta(days=12)), "last_status": "done",
                "last_finding": "Synthetic finding", "last_finding_shop_id": shops[0],
                "last_error": "", "last_skip_day": "", "last_missed_day": "",
                "updated_at": _iso(DAY0 + timedelta(days=12)),
            }])
            _write(conn, s.routine_runs, [{
                "tenant_id": tenant, "routine": "morning_brief", "day": f"2026-11-0{d}",
                "status": "done", "owner": "synthetic", "started_at": _iso(DAY0),
                "started_ts": DAY0.timestamp(), "finished_at": _iso(DAY0),
                "findings": 1, "result": "{}", "error": "",
            } for d in range(1, 10)])

            # A few returns so the ledger list has rows to order.
            returns: list[dict[str, Any]] = []
            for i in range(0, scale.shops_per_tenant, 40):
                rid = f"ret-{tenant}-{i:05d}"
                returns.append({
                    "tenant_id": tenant, "return_id": rid, "shop_id": shops[i],
                    "source": "slow_stock", "approval_id": None,
                    "reason": "Synthetic slow stock", "status": "drafted",
                    "value_drafted_kobo": 60_000, "value_collected_kobo": 0,
                    "value_returned_kobo": 0, "recovered_kobo": 0,
                    "pickup_cost_kobo": None, "saving_kobo": None, "credit_kobo": None,
                    "credit_approval_id": None,
                    "created_at": _iso(DAY0 + timedelta(days=5)),
                    "created_ts": (DAY0 + timedelta(days=5)).timestamp(),
                    "updated_at": _iso(DAY0 + timedelta(days=5)),
                })
                if ti == 0:
                    data.return_id = rid
            _write(conn, s.returns, returns)
    return data


# --------------------------------------------------------------- measurement


@dataclass
class Recorded:
    label: str
    statement: str
    parameters: Any
    elapsed_ms: float


class Recorder:
    """Times and keeps every statement an engine runs while a label is set.

    Used by the audit to explain the SQL the real code emitted (never a
    hand-written copy) and by the budget test to flag a statement over its
    budget.
    """

    def __init__(self, engine: Engine, *, keep: int = 100_000) -> None:
        self.engine = engine
        self.label = ""
        self.rows: list[Recorded] = []
        self._keep = keep
        self._started: dict[int, float] = {}
        self._listeners: list[tuple[Any, Any, Any]] = []

    def __enter__(self) -> Recorder:
        ev = sa.event

        def before(conn: Any, cursor: Any, statement: str, parameters: Any,
                   context: Any, executemany: bool) -> None:
            self._started[id(conn.connection)] = time.perf_counter()

        def after(conn: Any, cursor: Any, statement: str, parameters: Any,
                  context: Any, executemany: bool) -> None:
            started = self._started.pop(id(conn.connection), None)
            elapsed = (time.perf_counter() - started) * 1000 if started else 0.0
            if len(self.rows) < self._keep:
                self.rows.append(Recorded(self.label or "unlabelled", statement,
                                          parameters, elapsed))

        ev.listen(self.engine, "before_cursor_execute", before)
        ev.listen(self.engine, "after_cursor_execute", after)
        self._listeners = [(before, after, None)]
        return self

    def __exit__(self, *exc: Any) -> None:
        before, after, _ = self._listeners[0]
        sa.event.remove(self.engine, "before_cursor_execute", before)
        sa.event.remove(self.engine, "after_cursor_execute", after)
        self._listeners = []

    def selects(self) -> list[Recorded]:
        """One record per (label, statement): the plan we need to explain."""
        seen: set[tuple[str, str]] = set()
        out: list[Recorded] = []
        for r in self.rows:
            head = r.statement.lstrip().split(None, 1)[0].upper() if r.statement.strip() else ""
            if head not in ("SELECT", "(SELECT"):
                continue
            key = (r.label, " ".join(r.statement.split()))
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        return out


def explain(engine: Engine, rec: Recorded, *, analyze: bool = True,
            timeout_s: int = 30) -> str:
    """The plan for one recorded statement, on a rolled-back transaction."""
    head = "EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " if analyze else "EXPLAIN (FORMAT TEXT) "
    params = rec.parameters
    if isinstance(params, dict):
        params = {k: v for k, v in params.items()}
    with engine.connect() as conn:
        tx = conn.begin()
        try:
            if conn.dialect.name == "postgresql":
                conn.exec_driver_sql(f"SET LOCAL statement_timeout = '{timeout_s}s'")
            rows = conn.exec_driver_sql(head + rec.statement, params).all()
            plan = "\n".join(str(r[0]) for r in rows)
        finally:
            tx.rollback()
    return plan


def seq_scans(plan: str) -> list[str]:
    """The sequential-scan nodes in a text plan, for the audit summary."""
    out = []
    for line in plan.splitlines():
        stripped = line.strip()
        if stripped.startswith("Seq Scan on") or stripped.startswith("Parallel Seq Scan on"):
            out.append(stripped.split("(")[0].strip())
    return out
