"""Load the synthetic Coral Lane Trading world into the store.

    python -m mimi.store seed                 # DATABASE_URL, or sqlite:///./mimi.db
    python -m mimi.store seed --reset         # drop this tenant's rows first

All of it is synthetic: Coral Lane Trading, its shops, people, phone numbers
and accounts are fictional (``mimi/world``). Every row is marked
``simulated``. Delivery totals are added up here, in code, from the lines.
"""

from __future__ import annotations

import time
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.store import schema as s
from mimi.store.util import dumps, now_iso
from mimi.world.model import World

# The fictional owner's number (the same one the router eval uses).
DEMO_OWNER_PHONE = "+2340000000009"


class SeedError(RuntimeError):
    pass


def seed_world(engine: Engine, world: World, *, tenant_id: str | None = None,
               name: str | None = None, owner_phones: tuple[str, ...] = (DEMO_OWNER_PHONE,),
               reset: bool = False, daily_cap_kobo: int = 50_000) -> dict[str, int]:
    """Write the world under ``tenant_id`` (default: the world's own). Returns row counts."""
    tid = tenant_id or world.tenant_id
    counts: dict[str, int] = {}
    with engine.begin() as conn:
        exists = conn.execute(sa.select(s.tenants.c.tenant_id).where(
            s.tenants.c.tenant_id == tid)).first() is not None
        if exists and not reset:
            raise SeedError(f"tenant {tid!r} is already seeded (use --reset)")
        if exists:
            for t in (*s.BOOK_TABLES, s.threads, s.inbound_messages, s.messages,
                      s.thread_events, s.intents, s.approvals, s.credit_requests, s.spend,
                      s.asked_once, s.kill_switches, s.history, s.prefixes, s.artifacts):
                conn.execute(t.delete().where(t.c.tenant_id == tid))
            ids = sa.select(s.turns.c.turn_id).where(s.turns.c.tenant_id == tid)
            conn.execute(s.turn_attempts.delete().where(s.turn_attempts.c.turn_id.in_(ids)))
            conn.execute(s.turns.delete().where(s.turns.c.tenant_id == tid))
            conn.execute(s.tenants.delete().where(s.tenants.c.tenant_id == tid))

        def put(table: sa.Table, rows: list[dict[str, Any]]) -> None:
            if rows:
                conn.execute(table.insert(), rows)
            counts[table.name] = len(rows)

        put(s.tenants, [{
            "tenant_id": tid, "name": name or world.distributor,
            "owner_phones": dumps(sorted(owner_phones)), "owner_thread": f"{tid}:owner",
            "live_date": world.date_of(world.live_day).isoformat(),
            "daily_cap_kobo": daily_cap_kobo,
            "collection_account": dumps({"account_name": world.distributor.upper(),
                                         "bank": "Demo Bank", "account_number": "0000000000"}),
            "simulated": True, "created_at": now_iso()}])
        put(s.reps, [{"tenant_id": tid, "rep_id": r.rep_id, "name": r.name, "phone": r.phone,
                      "areas": dumps(list(r.areas))} for r in world.reps])
        put(s.shops, [{
            "tenant_id": tid, "shop_id": x.shop_id, "name": x.name, "shop_type": x.shop_type,
            "size": x.size, "owner": x.owner, "area": x.area, "street": x.street,
            "phone": x.phone, "rep_id": x.rep_id, "credit_limit_kobo": x.credit_limit_kobo,
            "bank": x.bank, "restock_days": x.restock_days, "simulated": True}
            for x in world.shops])
        put(s.skus, [{"tenant_id": tid, "sku": k.sku, "name": k.name, "brand": k.brand,
                      "unit": k.unit, "price_kobo": k.price_kobo,
                      "risen_price_kobo": k.risen_price_kobo} for k in world.skus])
        drows: list[dict[str, Any]] = []
        lrows: list[dict[str, Any]] = []
        for live, ds in ((False, world.deliveries), (True, world.live_deliveries)):
            for d in ds:
                lines: list[dict[str, Any]] = [{"tenant_id": tid, "delivery_id": d.delivery_id, "line_no": i,
                          "sku": ln.sku, "name": ln.name, "unit": ln.unit,
                          "quantity": ln.quantity, "unit_price_kobo": ln.unit_price_kobo,
                          "line_total_kobo": ln.quantity * ln.unit_price_kobo}
                         for i, ln in enumerate(d.lines)]
                total = sum(ln.quantity * ln.unit_price_kobo for ln in d.lines)
                if total != d.total_kobo:
                    raise SeedError(f"{d.delivery_id}: lines add up to {total}, "
                                    f"the world says {d.total_kobo}")
                lrows.extend(lines)
                drows.append({"tenant_id": tid, "delivery_id": d.delivery_id,
                              "shop_id": d.shop_id, "rep_id": d.rep_id, "day": d.day,
                              "delivered_on": d.delivered_on, "minute": d.minute,
                              "total_kobo": total, "status": "delivered",
                              "witnessed_at": None, "live": live})
        put(s.deliveries, drows)
        put(s.delivery_lines, lrows)
        put(s.receivables, [{
            "tenant_id": tid, "delivery_id": r.delivery_id, "shop_id": r.shop_id,
            "due_day": r.due_day, "due_on": r.due_on, "total_kobo": r.total_kobo,
            "paid_kobo": r.paid_kobo, "outstanding_kobo": r.outstanding_kobo,
            "status": r.status, "position": i} for i, r in enumerate(world.receivables)])
        put(s.payments, [{
            "tenant_id": tid, "payment_id": p.payment_id, "shop_id": p.shop_id,
            "delivery_id": p.delivery_id, "day": p.day, "paid_on": p.paid_on,
            "amount_kobo": p.amount_kobo, "method": p.method, "reference": p.reference,
            "status": p.status, "partial": p.partial, "split_of": p.split_of,
            "fake_alert": p.fake_alert} for p in world.payments])
        put(s.bank_feed, [{
            "tenant_id": tid, "credit_id": c.credit_id, "payment_id": c.payment_id,
            # As the world writes it (WAT, no offset), so tools read it as FakeGo gives it.
            "amount_kobo": c.amount_kobo, "received_at": c.received_at,
            "narration": c.narration}
            for c in world.bank_feed])
        put(s.promises, [{
            "tenant_id": tid, "promise_id": f"prm-{i:04d}", "shop_id": p.shop_id,
            "delivery_id": p.delivery_id, "made_on": world.date_of(p.made_day).isoformat(),
            "promised_on": world.date_of(p.promised_day).isoformat(), "kept": p.kept,
            "text": p.text} for i, p in enumerate(world.promises)])
        put(s.disputes, [{
            "tenant_id": tid, "dispute_id": f"dsp-{i:04d}", "shop_id": d.shop_id,
            "delivery_id": d.delivery_id, "day": d.day, "sku": d.sku, "missing": d.missing,
            "text": d.text, "status": d.status} for i, d in enumerate(world.disputes)])
        ts = time.time()
        put(s.threads, [{
            "tenant_id": tid, "thread_id": f"{tid}:owner", "kind": "owner", "shop_id": "",
            "state": "{}", "summary": "", "claim_seq": 0, "last_at": now_iso(ts),
            "last_ts": ts, "created_at": now_iso(ts)}])
    return counts
