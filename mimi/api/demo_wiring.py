"""The five demo scenes, wired end to end in code (2 Nov 2026).

Everything here is code over the book: no model call, no money moved.

* **Credit limits** (``POST /v1/credit/{shop_id}/limit`` and its undo): the
  owner sets a limit; each change is a ``thread_events`` row on the shop's
  thread (``type`` ``limit_change``), and an undo puts the old limit back.
* **Overdue and proof on reads**: whole-naira figures, days overdue and the
  demo's proof grade (``verified | estimated | unverified``) beside the
  stored ``proof_level`` (``verified | likely | estimated``).
* **Morning brief** (routine ``morning_brief``): the top overdue shops by
  name and ₦, written by code on the owner's thread.
* **Screenshot to verified** (behind ``MIMI_DEMO_WIRING``): a screenshot
  Proof records as likely becomes a payment row at once; a ``bank_credit``
  webhook then verifies it by code, applies it to the invoice, tells the
  shop and the owner, and opens a ``zoho_payment`` approval.
* **Zoho payment write**: approving records the payment in Zoho Books
  through ``mimi.connectors.zoho.payments`` (a fake Zoho kept in the store
  when there is no live connection, and always in demo mode); undo calls
  its delete path.
* **Over the limit** (behind ``MIMI_DEMO_WIRING``): a shop order that would
  take the shop past its limit opens a ``credit_over_limit`` approval with
  a limit code suggests and the reasons behind it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import date, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi import owed
from mimi.api.agent import Conflict, NotFound
from mimi.clock import wall
from mimi.connectors.base import ConnectorError
from mimi.connectors.composio import FakeComposio
from mimi.connectors.zoho import payments as zp
from mimi.provenance import parse_witnesses, payment_stamp, proof_word
from mimi.store import schema as s
from mimi.store.book import SqlGo, approval_id_for
from mimi.store.util import dumps, insert_ignore, loads, now_iso

ZOHO_KIND = "zoho_payment"
OVER_LIMIT_KIND = "credit_over_limit"
WIRING_KINDS = (ZOHO_KIND, OVER_LIMIT_KIND)

# The demo's words for the stored proof levels.
PROOF_GRADE = {"verified": "verified", "likely": "estimated", "estimated": "unverified"}


def ngn(kobo: int) -> int:
    """Whole naira from kobo (half a naira rounds up)."""
    return int((int(kobo) + 50) // 100)


def grade(level: str) -> str:
    return PROOF_GRADE.get(level, "unverified")


def _owner_thread(conn: sa.Connection, tenant: str) -> str:
    t = conn.execute(sa.select(s.tenants.c.owner_thread).where(
        s.tenants.c.tenant_id == tenant)).scalar()
    return str(t or f"{tenant}:owner")


def _ensure_thread(conn: sa.Connection, tenant: str, thread: str, kind: str,
                   shop_id: str, now: float) -> None:
    insert_ignore(conn, s.threads, {
        "tenant_id": tenant, "thread_id": thread, "kind": kind, "shop_id": shop_id,
        "state": "{}", "summary": "", "claim_seq": 0, "last_at": now_iso(now),
        "last_ts": now, "created_at": now_iso(now)})


def _say(conn: sa.Connection, tenant: str, thread: str, *, key: str, channel: str,
         shop_id: str, text: str, kind: str, turn_id: str = "", now: float) -> None:
    """One code-written message (passes the same style guard as every message)."""
    from mimi.harness.style import clean_reply

    insert_ignore(conn, s.messages, {
        "tenant_id": tenant, "message_id": f"out:{key}", "thread_id": thread,
        "turn_id": turn_id, "direction": "out", "sender": "mimi", "channel": channel,
        "shop_id": shop_id, "text": clean_reply(text, where=kind), "kind": kind,
        "status": "ready", "idempotency_key": key, "simulated": False,
        "at": now_iso(now), "ts": now})
    conn.execute(s.threads.update().where(
        s.threads.c.tenant_id == tenant, s.threads.c.thread_id == thread)
        .values(last_at=now_iso(now), last_ts=now))


def _shop(conn: sa.Connection, tenant: str, shop_id: str) -> Any:
    row = conn.execute(sa.select(s.shops).where(
        s.shops.c.tenant_id == tenant, s.shops.c.shop_id == shop_id)).first()
    if row is None:
        raise NotFound(shop_id)
    return row


# ------------------------------------------------------------ credit limits


def _limit_event(conn: sa.Connection, tenant: str, shop_id: str, *, event_id: str,
                 etype: str, old: int, new: int, reason: str, by: str, now: float,
                 extra: dict[str, Any] | None = None) -> None:
    thread = f"{tenant}:shop:{shop_id}"
    _ensure_thread(conn, tenant, thread, "shop", shop_id, now)
    verb = "undone" if etype == "limit_change_undo" else "set"
    insert_ignore(conn, s.thread_events, {
        "tenant_id": tenant, "event_id": event_id, "thread_id": thread, "run_id": "",
        "turn_id": "", "seq": 0, "step": 0, "type": etype, "tool": "set_credit_limit",
        "tier": "owner",
        "short": f"Credit limit {owed.naira(old)} to {owed.naira(new)} ({by}, {verb})",
        "cost_kobo": 0, "model": "", "duration_ms": 0.0,
        "data": dumps({"shop_id": shop_id, "old_kobo": old, "new_kobo": new,
                       "reason": reason, "by": by, **(extra or {})}),
        "at": now_iso(now), "ts": now})


def set_limit(engine: Engine, tenant: str, shop_id: str, amount_ngn: int, *,
              reason: str = "", by: str = "owner",
              source: str = "") -> dict[str, Any]:
    """The owner sets a shop's credit limit; recorded so it can be undone."""
    new = int(amount_ngn) * 100
    now = wall()
    with engine.begin() as conn:
        row = _shop(conn, tenant, shop_id)
        old = int(row.credit_limit_kobo)
        seed = f"{tenant}\x00{shop_id}\x00{old}\x00{new}\x00{now}\x00{source}"
        change_id = "lch_" + hashlib.sha256(seed.encode()).hexdigest()[:20]
        conn.execute(s.shops.update().where(
            s.shops.c.tenant_id == tenant, s.shops.c.shop_id == shop_id)
            .values(credit_limit_kobo=new))
        _limit_event(conn, tenant, shop_id, event_id=change_id, etype="limit_change",
                     old=old, new=new, reason=reason[:500], by=by, now=now,
                     extra={"source": source, "undone_at": None})
    return {"shop_id": shop_id, "old_limit_ngn": ngn(old), "new_limit_ngn": ngn(new),
            "change_id": change_id}


def undo_limit(engine: Engine, tenant: str, change_id: str) -> dict[str, Any]:
    now = wall()
    with engine.begin() as conn:
        ev = conn.execute(sa.select(s.thread_events).where(
            s.thread_events.c.tenant_id == tenant, s.thread_events.c.event_id == change_id,
            s.thread_events.c.type == "limit_change")).first()
        if ev is None:
            raise NotFound(change_id)
        data = loads(ev.data, {})
        if data.get("undone_at"):
            raise Conflict("this limit change was already undone")
        shop_id = str(data["shop_id"])
        row = _shop(conn, tenant, shop_id)
        current, restore = int(row.credit_limit_kobo), int(data["old_kobo"])
        conn.execute(s.shops.update().where(
            s.shops.c.tenant_id == tenant, s.shops.c.shop_id == shop_id)
            .values(credit_limit_kobo=restore))
        data["undone_at"] = now_iso(now)
        conn.execute(s.thread_events.update().where(
            s.thread_events.c.tenant_id == tenant, s.thread_events.c.event_id == change_id)
            .values(data=dumps(data)))
        _limit_event(conn, tenant, shop_id, event_id=f"{change_id}:undo",
                     etype="limit_change_undo", old=current, new=restore,
                     reason="undo", by="owner", now=now, extra={"change_id": change_id})
    return {"shop_id": shop_id, "old_limit_ngn": ngn(current),
            "new_limit_ngn": ngn(restore), "change_id": change_id, "undone": True}


# ------------------------------------------------------------ overdue on reads


def overdue_by_shop(engine: Engine, tenant: str) -> dict[str, owed.ShopOwed]:
    return owed.totals(SqlGo(engine).receivables(tenant, None)).shops


def overdue_fields(b: owed.ShopOwed | None) -> dict[str, int]:
    if b is None:
        return {"overdue_ngn": 0, "overdue_days": 0}
    return {"overdue_ngn": ngn(b.overdue_kobo),
            "overdue_days": int(b.oldest_days_overdue) if b.overdue_kobo > 0 else 0}


def payment_extras(p: dict[str, Any], shop: owed.ShopOwed | None) -> dict[str, Any]:
    return {"amount_ngn": ngn(int(p.get("amount_kobo") or 0)),
            "proof_grade": grade(str(p.get("proof_level") or "estimated")),
            **overdue_fields(shop)}


def recent_payments(engine: Engine, tenant: str, limit: int = 50) -> list[dict[str, Any]]:
    shops = overdue_by_shop(engine, tenant)
    with engine.connect() as conn:
        names = {r.shop_id: r.name for r in conn.execute(sa.select(
            s.shops.c.shop_id, s.shops.c.name).where(s.shops.c.tenant_id == tenant))}
        rows = conn.execute(sa.select(s.payments).where(s.payments.c.tenant_id == tenant)
                            .order_by(s.payments.c.day.desc(), s.payments.c.payment_id.desc())
                            .limit(limit)).all()
    out = []
    for p in rows:
        level = p.proof_level or "estimated"
        amount = 0 if p.fake_alert else int(p.amount_kobo)
        out.append({
            "payment_id": p.payment_id, "shop_id": p.shop_id,
            "shop_name": names.get(p.shop_id, ""), "amount_ngn": ngn(amount),
            "paid_on": p.paid_on, "method": p.method, "proof_level": level,
            "proof_grade": grade(level),
            "proof": proof_word(level, parse_witnesses(p.witnesses)),
            **overdue_fields(shops.get(p.shop_id))})
    return out


# ------------------------------------------------------------ morning brief


BRIEF_ROUTINES = frozenset({"morning_brief", "mimi.morning_brief"})


def brief_text(engine: Engine, tenant: str, top: int = 3) -> str:
    """The morning brief's words: the top overdue shops by name and ₦."""
    shops = sorted((b for b in overdue_by_shop(engine, tenant).values() if b.overdue_kobo > 0),
                   key=lambda b: (-b.overdue_kobo, b.shop_id))
    if not shops:
        return "Good morning. No shop is overdue today."
    lines = [f"{b.shop_name} is {b.oldest_days_overdue} day"
             f"{'s' if b.oldest_days_overdue != 1 else ''} overdue on {owed.naira(b.overdue_kobo)}"
             for b in shops[:top]]
    total = sum(b.overdue_kobo for b in shops)
    return (f"Good morning. Top overdue: {'; '.join(lines)}. In all {owed.naira(total)} is "
            f"overdue across {len(shops)} shop{'s' if len(shops) != 1 else ''}.")


# ------------------------------------------------------ screenshot payments


def _pay_id(tenant: str, proof_key: str) -> str:
    return "pay-ss-" + hashlib.sha256(f"{tenant}\x00{proof_key}".encode()).hexdigest()[:16]


def record_screenshot_payment(conn: sa.Connection, tenant: str, rec: dict[str, Any]) -> None:
    """A screenshot Proof recorded as likely: a payment row at level likely."""
    p = dict(rec.get("payload") or {})
    shop_id = str(rec.get("shop_id") or p.get("shop_id") or "")
    amount = p.get("amount_kobo")
    if not shop_id or not isinstance(amount, int) or amount <= 0:
        return
    key = str(p.get("proof_id") or rec.get("idempotency_key") or "")
    pid = _pay_id(tenant, key)
    t = conn.execute(sa.select(s.tenants.c.live_day, s.tenants.c.live_date).where(
        s.tenants.c.tenant_id == tenant)).first()
    day = int(t.live_day or 0) if t else 0
    paid_on = str(p.get("paid_at") or "")[:10] or (str(t.live_date) if t else "")
    dl = list(p.get("delivery_ids") or [])
    insert_ignore(conn, s.payments, {
        "tenant_id": tenant, "payment_id": pid, "shop_id": shop_id,
        "delivery_id": str(dl[0]) if dl else "", "day": day, "paid_on": paid_on,
        "amount_kobo": amount, "method": "transfer",
        "reference": str(p.get("reference") or "")[:100], "status": "reported",
        "partial": False, "split_of": None, "fake_alert": False,
        **payment_stamp(pid, "transfer", fake_alert=False,
                        screenshot_id=key or pid).columns()})


def after_intents(conn: sa.Connection, tenant: str, records: list[dict[str, Any]],
                  outcomes: dict[str, str], now: float) -> list[str]:
    """Code that runs once a turn's intents are applied (``MIMI_DEMO_WIRING``).

    Returns the approval ids it opened."""
    opened: list[str] = []
    for rec in records:
        tool = str(rec.get("tool", ""))
        outcome = outcomes.get(str(rec.get("idempotency_key", "")), "")
        if tool == "record_payment_proof" and outcome == "applied":
            record_screenshot_payment(conn, tenant, rec)
        elif tool in ("submit_order", "revise_order") and outcome == "queued_for_approval":
            apr = over_limit_approval(conn, tenant, rec, now)
            if apr:
                opened.append(apr)
    return opened


def bank_credit(conn: sa.Connection, tenant: str, shop_id: str, *, event_id: str,
                amount_ngn: int | None, now: float, turn_id: str = "") -> dict[str, Any]:
    """A bank credit for ``shop_id``: verify the newest likely screenshot payment.

    Applies it to the shop's invoices (oldest matched first), tells the shop
    and the owner, and opens the ``zoho_payment`` approval."""
    shop = _shop(conn, tenant, shop_id)
    owner = _owner_thread(conn, tenant)
    shop_thread = f"{tenant}:shop:{shop_id}"
    _ensure_thread(conn, tenant, shop_thread, "shop", shop_id, now)
    q = sa.select(s.payments).where(
        s.payments.c.tenant_id == tenant, s.payments.c.shop_id == shop_id,
        s.payments.c.proof_level == "likely", s.payments.c.fake_alert.is_(False))
    if amount_ngn:
        q = q.where(s.payments.c.amount_kobo == int(amount_ngn) * 100)
    pay = conn.execute(q.order_by(s.payments.c.day.desc(),
                                  s.payments.c.payment_id.desc())).first()
    if pay is None:
        _say(conn, tenant, owner, key=f"bank:{event_id}:none", channel="owner_console",
             shop_id="", turn_id=turn_id, kind="bank_credit_unmatched", now=now,
             text=(f"A bank credit came in for {shop.name}"
                   + (f" ({owed.naira(int(amount_ngn) * 100)})" if amount_ngn else "")
                   + ", but there is no screenshot payment waiting to match it. "
                     "Nothing has changed."))
        return {"matched": False}
    credit_id = "bc-" + hashlib.sha256(f"{tenant}\x00{event_id}".encode()).hexdigest()[:16]
    insert_ignore(conn, s.bank_feed, {
        "tenant_id": tenant, "credit_id": credit_id, "payment_id": pay.payment_id,
        "amount_kobo": int(pay.amount_kobo), "received_at": now_iso(now),
        "narration": f"bank credit for {shop_id}"})
    ws = [w for w in parse_witnesses(pay.witnesses) if w.kind == "alert_screenshot_checked"]
    stamp = payment_stamp(pay.payment_id, "transfer", fake_alert=False,
                          bank_credit_id=credit_id,
                          screenshot_id=ws[0].source_id if ws else "")
    conn.execute(s.payments.update().where(
        s.payments.c.tenant_id == tenant, s.payments.c.payment_id == pay.payment_id)
        .values(status="matched", **stamp.columns()))
    insert_ignore(conn, s.proof_events, {
        "tenant_id": tenant, "event_id": f"pe-{credit_id}", "record_type": "payment_transfer",
        "record_id": pay.payment_id, "old_level": "likely", "new_level": stamp.proof_level,
        "witness_kind": "bank_feed", "witness_source": credit_id,
        "policy_version": stamp.proof_policy_version, "at": now_iso(now)})
    invoice = _apply_to_invoices(conn, tenant, shop_id, int(pay.amount_kobo),
                                 str(pay.delivery_id or ""))
    money = owed.naira(int(pay.amount_kobo))
    _say(conn, tenant, shop_thread, key=f"bank:{event_id}:shop", channel="shop_whatsapp",
         shop_id=shop_id, turn_id=turn_id, kind="bank_credit_thanks", now=now,
         text=f"We have received your {money} payment. Thank you.")
    _say(conn, tenant, owner, key=f"bank:{event_id}:owner", channel="owner_console",
         shop_id="", turn_id=turn_id, kind="bank_credit_verified", now=now,
         text=(f"The bank confirmed {shop.name}'s {money} transfer, so it is now verified. "
               "Approve it to record it in Zoho Books."))
    apr = zoho_approval(conn, tenant, owner, shop, pay.payment_id, int(pay.amount_kobo),
                        invoice, str(pay.paid_on), turn_id, now)
    return {"matched": True, "payment_id": pay.payment_id, "approval_id": apr}


def _apply_to_invoices(conn: sa.Connection, tenant: str, shop_id: str, amount: int,
                       first: str) -> str:
    """Take a verified payment off the shop's open invoices (the matched one
    first, then oldest first). Returns the first invoice it paid."""
    rows = list(conn.execute(sa.select(s.receivables).where(
        s.receivables.c.tenant_id == tenant, s.receivables.c.shop_id == shop_id,
        s.receivables.c.outstanding_kobo > 0)
        .order_by(s.receivables.c.position, s.receivables.c.delivery_id)))
    rows.sort(key=lambda r: 0 if r.delivery_id == first else 1)
    left, paid_first = amount, ""
    for r in rows:
        if left <= 0:
            break
        take = min(left, int(r.outstanding_kobo))
        out = int(r.outstanding_kobo) - take
        conn.execute(s.receivables.update().where(
            s.receivables.c.tenant_id == tenant,
            s.receivables.c.delivery_id == r.delivery_id)
            .values(paid_kobo=int(r.paid_kobo) + take, outstanding_kobo=out,
                    status="paid" if out == 0 else r.status))
        paid_first = paid_first or r.delivery_id
        left -= take
    return paid_first or first


def invoice_number(delivery_id: str) -> str:
    """The Zoho invoice number the demo books use for a delivery."""
    return "INV-" + delivery_id.removeprefix("dlv-") if delivery_id else ""


def zoho_approval(conn: sa.Connection, tenant: str, owner_thread: str, shop: Any,
                  payment_id: str, amount: int, delivery_id: str, paid_on: str,
                  turn_id: str, now: float) -> str:
    key = f"zoho_payment:{payment_id}"
    apr = approval_id_for(tenant, key)
    payload = {
        "kind": ZOHO_KIND, "shop_id": shop.shop_id, "shop_name": shop.name,
        "amount_ngn": ngn(amount), "amount_kobo": amount,
        "invoice_number": invoice_number(delivery_id), "delivery_id": delivery_id,
        "reference_number": zp.reference_for(key), "payment_id": payment_id,
        "date": paid_on, "customer_id": f"zc-{shop.shop_id}",
        "invoices": ([{"invoice_id": invoice_number(delivery_id), "amount_applied_kobo": amount}]
                     if delivery_id else [])}
    insert_ignore(conn, s.approvals, {
        "approval_id": apr, "tenant_id": tenant, "idempotency_key": key,
        "turn_id": turn_id, "thread_id": owner_thread, "helper": "", "skill": "proof/bank-credit",
        "shop_id": shop.shop_id, "tool": ZOHO_KIND, "intent_kind": ZOHO_KIND,
        "preview": zp.preview_payment(payload), "payload": dumps(payload),
        "status": "pending", "created_at": now_iso(now), "created_ts": now, "handling": ""})
    return apr


# ------------------------------------------------------------ fake Zoho


class StoreZoho(FakeComposio):
    """A deterministic Zoho Books that keeps its payments in the store
    (``artifacts`` rows ``zoho-fake:<tenant>:<payment_id>``). It never calls
    Zoho and never moves money; it answers like Zoho's own API."""

    def __init__(self, engine: Engine, tenant: str) -> None:
        super().__init__()
        self.engine, self.tenant = engine, tenant

    def _path(self, payment_id: str) -> str:
        return f"zoho-fake:{self.tenant}:{payment_id}"

    def proxy(self, name: str, *, method: str, endpoint: str,
              connected_account_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"action": f"proxy:{name}", "method": method,
                           "endpoint": endpoint, "arguments": dict(body)})
        prefix = f"zoho-fake:{self.tenant}:"
        with self.engine.begin() as conn:
            if name == "customer_payment_search":
                ref = str(body.get("reference_number", ""))
                found = []
                for r in conn.execute(sa.select(s.artifacts).where(
                        s.artifacts.c.tenant_id == self.tenant,
                        s.artifacts.c.path.startswith(prefix, autoescape=True))):
                    p = loads(r.content, {})
                    if p.get("reference_number") == ref and not p.get("deleted"):
                        found.append(p)
                return {"code": 0, "customer_payments": found}
            if name == "customer_payment_create":
                ref = str(body.get("reference_number", ""))
                pid = "zfake_" + hashlib.sha256(f"{self.tenant}\x00{ref}".encode()
                                                ).hexdigest()[:16]
                payment = {**body, "payment_id": pid}
                insert_ignore(conn, s.artifacts, {"path": self._path(pid),
                                                  "tenant_id": self.tenant,
                                                  "content": json.dumps(payment,
                                                                        sort_keys=True)})
                return {"code": 0, "payment": payment}
            if name == "customer_payment_delete":
                pid = str(body.get("payment_id", ""))
                row = conn.execute(sa.select(s.artifacts).where(
                    s.artifacts.c.path == self._path(pid))).first()
                if row is None:
                    return {"code": 1002, "message": "payment not found"}
                p = loads(row.content, {})
                p["deleted"] = True
                conn.execute(s.artifacts.update().where(s.artifacts.c.path == self._path(pid))
                             .values(content=json.dumps(p, sort_keys=True)))
                return {"code": 0, "message": "The payment has been deleted."}
        return {"code": 9, "message": f"unknown call {name}"}


def _zoho(engine: Engine, tenant: str, demo_mode: bool) -> tuple[Any, str, str, bool]:
    """(transport, connected account id, organization id, is_fake).

    Same path for a sandbox (demo) tenant and a production one: with an
    active Zoho Books connection, the real Composio transport and the
    organization id, the write goes to that Zoho account. Only a tenant
    with no real connection uses the in-store Zoho (sample data).
    ``demo_mode`` is kept for callers; it no longer changes the path."""
    del demo_mode
    from mimi.connectors.env import ConnectorSettings

    with engine.connect() as conn:
        acct = conn.execute(sa.select(s.connector_accounts).where(
            s.connector_accounts.c.tenant_id == tenant,
            s.connector_accounts.c.toolkit == "ZOHO_BOOKS",
            s.connector_accounts.c.status == "active")).first()
    if acct is not None and ConnectorSettings.from_env().live:
        from mimi.connectors.composio import ComposioTransport

        transport = ComposioTransport()
        # Composio addresses the account by its own id, not our connection id.
        account = str(acct.connected_account_id or acct.connection_id)
        org = os.environ.get("ZOHO_BOOKS_ORGANIZATION_ID", "").strip() or _zoho_org(transport, tenant, account)
        if org:
            return transport, account, org, False
    return StoreZoho(engine, tenant), "demo-zoho", "demo-org", True


def _zoho_org(transport: Any, tenant: str, account: str) -> str:
    """The connected Zoho Books account's first organization id ('' when it
    has none or the read fails), so each distributor writes to its own books."""
    from mimi.connectors.zoho.books import ZohoBooksConnector

    try:
        rows = ZohoBooksConnector(transport, tenant_id=tenant, connection_id=account).read(
            "zoho_books_organizations").rows
    except ConnectorError:
        return ""
    return str(rows[0].values.get("organization_id") or "") if rows else ""


def _set_payload(conn: sa.Connection, tenant: str, approval_id: str,
                 payload: dict[str, Any]) -> None:
    conn.execute(s.approvals.update().where(
        s.approvals.c.tenant_id == tenant, s.approvals.c.approval_id == approval_id)
        .values(payload=dumps(payload)))


def on_approved(engine: Engine, tenant: str, a: dict[str, Any], *,
                demo_mode: bool) -> None:
    """Run the code action an approved wiring approval stands for."""
    payload = dict(a.get("payload") or {})
    kind = payload.get("kind")
    now = wall()
    if kind == ZOHO_KIND:
        transport, conn_id, org, fake = _zoho(engine, tenant, demo_mode)
        try:
            res = zp.record_payment(transport=transport, tenant_id=tenant,
                                    connection_id=conn_id, organization_id=org,
                                    payload=payload,
                                    idempotency_key=f"zoho_payment:{payload['payment_id']}",
                                    approval_id=a["approval_id"])
        except ConnectorError as exc:
            payload["result"] = {"error": str(exc), "undo_available": False, "demo": fake}
            with engine.begin() as conn:
                _set_payload(conn, tenant, a["approval_id"], payload)
            return
        payload["result"] = {"zoho_payment_id": res["payment_id"], "undo_available": True,
                             "demo": fake}
        with engine.begin() as conn:
            _set_payload(conn, tenant, a["approval_id"], payload)
            _say(conn, tenant, _owner_thread(conn, tenant),
                 key=f"zoho:{a['approval_id']}:done", channel="owner_console", shop_id="",
                 kind="zoho_payment_recorded", now=now,
                 text=(f"Recorded {payload['shop_name']}'s "
                       f"{owed.naira(int(payload['amount_kobo']))} payment in Zoho Books"
                       + (f" against {payload['invoice_number']}"
                          if payload.get("invoice_number") else "")
                       + ". You can undo it."))
    elif kind == OVER_LIMIT_KIND:
        res = set_limit(engine, tenant, str(payload["shop_id"]),
                        int(payload["suggested_limit_ngn"]),
                        reason="Credit's suggestion, approved by the owner",
                        source=a["approval_id"])
        payload["result"] = {**res, "undo_available": True}
        with engine.begin() as conn:
            _set_payload(conn, tenant, a["approval_id"], payload)


def undo_approval(engine: Engine, tenant: str, approval_id: str, *,
                  demo_mode: bool) -> None:
    with engine.connect() as conn:
        a = conn.execute(sa.select(s.approvals).where(
            s.approvals.c.tenant_id == tenant, s.approvals.c.approval_id == approval_id)).first()
    if a is None:
        raise NotFound(approval_id)
    payload = loads(a.payload, {})
    res = payload.get("result") or {}
    if payload.get("kind") == OVER_LIMIT_KIND and a.status == "approved" and res.get(
            "change_id") and res.get("undo_available"):
        undo_limit(engine, tenant, str(res["change_id"]))
        payload["result"] = {**res, "undo_available": False, "undone_at": now_iso(wall())}
        with engine.begin() as conn:
            _set_payload(conn, tenant, approval_id, payload)
        return
    if (payload.get("kind") != ZOHO_KIND or a.status != "approved"
            or not res.get("zoho_payment_id")):
        raise Conflict("only an approved Zoho payment or limit change can be undone")
    if not res.get("undo_available"):
        raise Conflict("this Zoho payment was already undone")
    transport, conn_id, org, _fake = _zoho(engine, tenant, demo_mode)
    try:
        zp.delete_payment(transport=transport, connection_id=conn_id, organization_id=org,
                          payment_id=str(res["zoho_payment_id"]))
    except ConnectorError as exc:
        raise Conflict(f"Zoho did not undo it: {exc}") from exc
    now = wall()
    payload["result"] = {**res, "undo_available": False, "undone_at": now_iso(now)}
    with engine.begin() as conn:
        _set_payload(conn, tenant, approval_id, payload)
        _say(conn, tenant, _owner_thread(conn, tenant), key=f"zoho:{approval_id}:undo",
             channel="owner_console", shop_id="", kind="zoho_payment_undone", now=now,
             text=(f"Undone: {payload.get('shop_name', 'the shop')}'s payment is no longer "
                   "in Zoho Books."))


# ------------------------------------------------------------ over the limit


SUGGEST_STEP_KOBO = 50_000 * 100
WINDOW_DAYS = 90


def _shop_owed(conn: sa.Connection, tenant: str, shop_id: str,
               name: str) -> owed.ShopOwed | None:
    """One shop's owed and overdue, read inside ``conn`` (as ``SqlGo.receivables``)."""
    t = conn.execute(sa.select(s.tenants.c.live_date).where(
        s.tenants.c.tenant_id == tenant)).scalar()
    today = date.fromisoformat(str(t)) if t else date.max
    rows = [{"shop_id": r.shop_id, "shop_name": name, "due_on": r.due_on,
             "days_overdue": max(0, (today - date.fromisoformat(r.due_on)).days),
             "outstanding_kobo": int(r.outstanding_kobo), "status": r.status}
            for r in conn.execute(sa.select(s.receivables).where(
                s.receivables.c.tenant_id == tenant, s.receivables.c.shop_id == shop_id,
                s.receivables.c.outstanding_kobo > 0))]
    return owed.totals(rows).shops.get(shop_id)


def suggest_limit(conn: sa.Connection, tenant: str, shop: Any, owed_kobo: int,
                  order_kobo: int, overdue: owed.ShopOwed | None
                  ) -> tuple[int, list[str]]:
    """A limit that covers this order, rounded up to ₦50,000, and the reasons.

    Code only, from the book: what the shop owes, its bank-verified payment
    record over 90 days, and what is overdue."""
    limit = int(shop.credit_limit_kobo)
    need = owed_kobo + order_kobo
    suggested = max(limit, int(math.ceil(need / SUGGEST_STEP_KOBO)) * SUGGEST_STEP_KOBO)
    t = conn.execute(sa.select(s.tenants.c.live_date).where(
        s.tenants.c.tenant_id == tenant)).scalar()
    since = (date.fromisoformat(str(t)) - timedelta(days=WINDOW_DAYS)).isoformat() if t else ""
    pays = conn.execute(sa.select(s.payments).where(
        s.payments.c.tenant_id == tenant, s.payments.c.shop_id == shop.shop_id,
        s.payments.c.paid_on >= since)).all()
    real = [p for p in pays if not p.fake_alert]
    verified = sum(1 for p in real if p.proof_level == "verified")
    paid = sum(int(p.amount_kobo) for p in real)
    reasons = [f"Owes {owed.naira(owed_kobo)} now; this order takes it to "
               f"{owed.naira(need)}, {owed.naira(max(0, need - limit))} over its "
               f"{owed.naira(limit)} limit."]
    if real:
        reasons.append(f"{verified} of {len(real)} payments in the last {WINDOW_DAYS} days "
                       "were confirmed by the bank.")
        reasons.append(f"Paid {owed.naira(paid)} in the last {WINDOW_DAYS} days.")
    else:
        reasons.append(f"No payments in the last {WINDOW_DAYS} days.")
    if overdue is not None and overdue.overdue_kobo > 0:
        reasons.append(f"{owed.naira(overdue.overdue_kobo)} is overdue, oldest "
                       f"{overdue.oldest_days_overdue} days late.")
    else:
        reasons.append("Nothing is overdue.")
    return suggested, reasons


def over_limit_approval(conn: sa.Connection, tenant: str, rec: dict[str, Any],
                        now: float) -> str:
    p = dict(rec.get("payload") or {})
    if not p.get("over_credit"):
        return ""
    shop_id = str(rec.get("shop_id") or p.get("shop_id") or "")
    order_kobo = p.get("total_kobo")
    if not shop_id or not isinstance(order_kobo, int):
        return ""
    shop = _shop(conn, tenant, shop_id)
    one = _shop_owed(conn, tenant, shop_id, shop.name)
    owed_kobo = one.owed_kobo if one else 0
    suggested, reasons = suggest_limit(conn, tenant, shop, owed_kobo, order_kobo, one)
    order_id = str(p.get("order_id", ""))
    key = f"credit_over_limit:{order_id or rec.get('idempotency_key', '')}"
    apr = approval_id_for(tenant, key)
    payload = {
        "kind": OVER_LIMIT_KIND, "shop_id": shop_id, "shop_name": shop.name,
        "order_id": order_id, "order_ngn": ngn(order_kobo),
        "limit_ngn": ngn(int(shop.credit_limit_kobo)), "owed_ngn": ngn(owed_kobo),
        "suggested_limit_ngn": ngn(suggested), "reasons": reasons}
    insert_ignore(conn, s.approvals, {
        "approval_id": apr, "tenant_id": tenant, "idempotency_key": key, "turn_id": "",
        "thread_id": _owner_thread(conn, tenant), "helper": "", "skill": "credit/credit-review",
        "shop_id": shop_id, "tool": OVER_LIMIT_KIND, "intent_kind": OVER_LIMIT_KIND,
        "preview": (f"{shop.name}'s order of {owed.naira(order_kobo)} is over its credit limit. "
                    f"Credit suggests a limit of {owed.naira(suggested)}."),
        "payload": dumps(payload), "status": "pending", "created_at": now_iso(now),
        "created_ts": now, "handling": ""})
    return apr
