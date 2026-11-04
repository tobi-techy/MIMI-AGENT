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
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi import owed
from mimi.api.agent import Conflict, NotFound
from mimi.clock import wall
from mimi.connectors.base import ConnectorError
from mimi.connectors.composio import ComposioTransport, FakeComposio
from mimi.connectors.zoho import payments as zp
from mimi.provenance import parse_witnesses, payment_stamp, proof_word
from mimi.store import invoices as invoice_reads
from mimi.store import on_account
from mimi.store import schema as s
from mimi.store.book import SqlGo, approval_id_for
from mimi.store.util import dumps, insert_ignore, loads, now_iso

ZOHO_KIND = "zoho_payment"
OVER_LIMIT_KIND = "credit_over_limit"
# The returns ledger's settlement credit (feat/returns-and-exposure). It rides
# this same approve/undo path, so the card behaves exactly like a Zoho payment.
RETURN_CREDIT_KIND = "return_credit"
WIRING_KINDS = (ZOHO_KIND, OVER_LIMIT_KIND, RETURN_CREDIT_KIND)

# One explicit switch for a real Zoho Books write. ``MIMI_ZOHO_LIVE_WRITE=1``
# lets ``_zoho`` return the real Composio transport when the tenant has an
# active ZOHO_BOOKS connection; off (CI, offline, and every run until the
# owner says GO REAL) the write stays in the store's fake Zoho and nothing
# else changes. The switch never auto-approves, never retries, and never
# widens what may be written: it only chooses where an approved write lands.
ZOHO_LIVE_WRITE_ENV = "MIMI_ZOHO_LIVE_WRITE"
ZOHO_LIVE_WRITE_MAX_ENV = "MIMI_ZOHO_LIVE_WRITE_MAX"


def zoho_live_write() -> bool:
    """True only when the owner switched on real Zoho Books writes."""
    return os.environ.get(ZOHO_LIVE_WRITE_ENV, "").strip().lower() in (
        "1", "true", "yes", "on")


def zoho_live_write_max() -> int:
    """How many real Zoho writes this tenant may make per Lagos day.

    Default 1 when live writing is on: unset never means unlimited,
    because an unlimited switch is the danger being guarded against.
    Raise it by setting the env var to 2 and retaking the shot the same
    day; the count below resets at Lagos midnight."""
    try:
        return max(1, int(os.environ.get(ZOHO_LIVE_WRITE_MAX_ENV, "").strip() or "1"))
    except ValueError:
        return 1


def zoho_live_writes_today(conn: sa.Connection, tenant: str, now: float) -> int:
    """Real Zoho writes recorded for this tenant since Lagos midnight.

    Real means the stored result says ``demo`` false and carries a
    payment id: fake and demo writes never count against the cap."""
    from datetime import datetime, timezone

    from mimi.tz import LAGOS, lagos_day

    today = lagos_day(datetime.fromtimestamp(now, tz=timezone.utc))
    start = datetime(today.year, today.month, today.day, tzinfo=LAGOS).timestamp()
    n = 0
    for row in conn.execute(sa.select(s.approvals.c.payload).where(
            s.approvals.c.tenant_id == tenant,
            s.approvals.c.tool == ZOHO_KIND,
            s.approvals.c.status == "approved",
            s.approvals.c.created_ts >= start,
            s.approvals.c.created_ts < start + 86400)):
        res = loads(row.payload, {}).get("result") or {}
        if res.get("zoho_payment_id") and res.get("demo") is False:
            n += 1
    return n

# The demo's words for the stored proof levels.
PROOF_GRADE = {"verified": "verified", "likely": "estimated", "estimated": "unverified"}

# The demo org's live Zoho Books account, for demo tenants only. When the
# deployed demo sets ``MIMI_DEMO_ZOHO_ACCOUNT`` to the owner's connected
# account id, every demo tenant reads (and, with the live-write switch on,
# writes) through that account: demo sessions are new tenants and nothing
# in the demo path ever runs the connect flow that would write a
# ``connector_accounts`` row, so without this the two-books read is
# ``zoho_connected: false`` forever. Gated on the demo wiring that is
# already there, so a real distributor still has to connect properly.
# Setting absent means today's behaviour exactly: not connected, no
# invented data.
DEMO_ZOHO_ACCOUNT_ENV = "MIMI_DEMO_ZOHO_ACCOUNT"


def demo_zoho_account() -> str:
    """The demo org's connected account id, or empty when unset."""
    return os.environ.get(DEMO_ZOHO_ACCOUNT_ENV, "").strip()


def _demo_wiring_on() -> bool:
    from mimi.flags import demo_wiring

    return demo_wiring()



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
    applied = _apply_to_invoices(conn, tenant, shop_id, int(pay.amount_kobo),
                                 str(pay.delivery_id or ""),
                                 payment_id=str(pay.payment_id))
    money = owed.naira(int(pay.amount_kobo))
    _say(conn, tenant, shop_thread, key=f"bank:{event_id}:shop", channel="shop_whatsapp",
         shop_id=shop_id, turn_id=turn_id, kind="bank_credit_thanks", now=now,
         text=f"We have received your {money} payment. Thank you.")
    _say(conn, tenant, owner, key=f"bank:{event_id}:owner", channel="owner_console",
         shop_id="", turn_id=turn_id, kind="bank_credit_verified", now=now,
         text=(f"The bank confirmed {shop.name}'s {money} transfer, so it is now verified. "
               "Approve it to record it in Zoho Books."))
    apr = zoho_approval(conn, tenant, owner, shop, pay.payment_id, int(pay.amount_kobo),
                        applied, str(pay.paid_on), turn_id, now)
    # What the invoices could not absorb is the shop's money held on its account,
    # recorded on the credit note row so the payments table and the receivables
    # add up (mimi.store.on_account). Nothing to record when it all applied, and
    # the part the payment's own delivery will take when it is billed is not on
    # the account either: that roll is where it lands (roll_will_take_kobo).
    on_account.record(conn, tenant=tenant, shop_id=shop_id, payment_id=pay.payment_id,
                      amount=int(pay.amount_kobo), applied_kobo=applied["applied_kobo"],
                      applications=applied["applications"],
                      waiting_kobo=applied["waiting_for_roll_kobo"], approval_id=apr, now=now)
    return {"matched": True, "payment_id": pay.payment_id, "approval_id": apr}


def _apply_to_invoices(conn: sa.Connection, tenant: str, shop_id: str, amount: int,
                       first: str, payment_id: str = "") -> dict[str, Any]:
    """Take a verified payment off the shop's open invoices (the matched one
    first, then oldest first).

    The invoices are read holding them (``mimi.store.invoices``), in the one
    order every writer takes them in: a return credit and the routine read the
    same rows the same way, so this payment cannot be computed from figures a
    credit commits over, and no two writers cycle on each other's locks. Sorting
    the rows below for the allocation (matched invoice first) happens after the
    lock is held, so it cannot change the order the locks were taken in.

    Returns what those invoices could absorb: ``{"first": delivery_id,
    "applied_kobo": int, "waiting_for_roll_kobo": int, "billed_by_roll_kobo":
    int, "on_account_kobo": int, "applications": [{"delivery_id",
    "amount_kobo"}]}``. Each invoice takes no more than its own open balance, so
    the applications never add up to more than the invoices owed, which is what
    the Zoho card has to say: an application bigger than the invoice is a real
    over-application in a real org. ``first`` is the first invoice that actually
    took something, or empty when none did, so nothing downstream names an
    invoice that got nothing.

    The payment's own delivery is handled first, whichever side of the roll it
    is on (``on_account.roll_share_kobo``). If the roll has already billed it,
    the roll spent this payment on that invoice and this function adds that
    application, naming the invoice, without touching an older one: Zoho is a
    separate book and needs the same application. If the delivery has not been
    billed, its part is ``waiting_for_roll_kobo``: the roll will credit it there,
    so it is applied nowhere and held nowhere. Only what is left after the
    delivery and the invoices is ``on_account_kobo``, for the caller to record on
    the shop's account. The parts add up to ``amount``."""
    share = on_account.roll_share_kobo(conn, tenant=tenant, delivery_id=first,
                                       payment_id=payment_id, amount=amount)
    applications: list[dict[str, Any]] = []
    paid_first = ""
    if share.billed and share.kobo > 0:
        applications.append({"delivery_id": str(first), "amount_kobo": share.kobo})
        paid_first = str(first)  # the invoice the roll already spent it on
    waiting = 0 if share.billed else share.kobo
    rows = list(invoice_reads.open_invoices(conn, tenant, shop_id, lock=True))
    rows.sort(key=lambda r: 0 if r.delivery_id == first else 1)
    left = int(amount) - share.kobo
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
        applications.append({"delivery_id": str(r.delivery_id), "amount_kobo": take})
        paid_first = paid_first or r.delivery_id
        left -= take
    return {"first": paid_first, "applied_kobo": int(amount) - waiting - max(0, left),
            "waiting_for_roll_kobo": waiting, "billed_by_roll_kobo": share.kobo
            if share.billed else 0, "on_account_kobo": max(0, left),
            "applications": applications}


def invoice_number(delivery_id: str) -> str:
    """The Zoho invoice number the demo books use for a delivery."""
    return "INV-" + delivery_id.removeprefix("dlv-") if delivery_id else ""


def zoho_approval(conn: sa.Connection, tenant: str, owner_thread: str, shop: Any,
                  payment_id: str, amount: int, applied: Mapping[str, Any], paid_on: str,
                  turn_id: str, now: float) -> str:
    """The card the owner approves to write the payment into Zoho Books.

    ``invoices`` names what came off each invoice, cents for cents, and never
    more than that invoice owed: ``applied`` is what
    ``_apply_to_invoices`` actually took, not the payment amount. When the
    payment was bigger than the shop owed, ``on_account_kobo`` carries the
    difference and the invoice list adds up to less than ``amount_kobo``, which
    is what Zoho records as an unapplied amount on the customer payment. When
    part of the payment belongs to a delivery that has not been billed yet,
    ``waiting_for_roll_kobo`` carries it: it is not on the account and not on an
    invoice, and ``invoice_number`` and ``delivery_id`` name the first invoice
    that did take something, so the card never names one that took nothing.
    ``billed_by_roll_kobo`` is the part the roll already spent on the payment's
    own invoice when it billed the delivery, and it is in ``invoices`` too, so
    the Zoho book gets the application the shop's own book already made."""
    key = f"zoho_payment:{payment_id}"
    apr = approval_id_for(tenant, key)
    first = str(applied.get("first") or "")
    invoices = [{"invoice_id": invoice_number(str(a.get("delivery_id") or "")),
                 "amount_applied_kobo": int(a.get("amount_kobo") or 0)}
                for a in applied.get("applications") or []]
    payload = {
        "kind": ZOHO_KIND, "shop_id": shop.shop_id, "shop_name": shop.name,
        "amount_ngn": ngn(amount), "amount_kobo": amount,
        "applied_kobo": int(applied.get("applied_kobo") or 0),
        "on_account_kobo": int(applied.get("on_account_kobo") or 0),
        "waiting_for_roll_kobo": int(applied.get("waiting_for_roll_kobo") or 0),
        "billed_by_roll_kobo": int(applied.get("billed_by_roll_kobo") or 0),
        "invoice_number": invoice_number(first), "delivery_id": first,
        "reference_number": zp.reference_for(key), "payment_id": payment_id,
        "date": paid_on, "customer_id": f"zc-{shop.shop_id}",
        "invoices": invoices}
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


def _zoho(engine: Engine, tenant: str, demo_mode: bool,
          transport: Any | None = None) -> tuple[Any, str, str, bool]:
    """(transport, connected account id, organization id, is_fake).

    The write goes to the real Zoho Books account only when the owner
    switched it on (``MIMI_ZOHO_LIVE_WRITE=1``) and the tenant has an
    active ZOHO_BOOKS connection on a live transport. Otherwise the
    tenant's write stays in the store's fake Zoho (sample data).
    ``demo_mode`` is kept for callers; it no longer changes the path.

    When a live write was asked for but the organization cannot be
    resolved, this raises instead of falling back to the fake books: a
    real tenant must never silently show demo numbers, and the approval
    result must say plainly where the payment landed."""
    del demo_mode
    from mimi.connectors.env import ConnectorSettings

    acct: Any = None
    with engine.connect() as conn:
        acct = conn.execute(sa.select(s.connector_accounts).where(
            s.connector_accounts.c.tenant_id == tenant,
            s.connector_accounts.c.toolkit == "ZOHO_BOOKS",
            s.connector_accounts.c.status == "active")).first()
    demo_account = demo_zoho_account() if _demo_wiring_on() else ""
    if acct is None and demo_account:
        # A demo tenant with the demo account set: read (and, with the
        # live-write switch on, write) through the owner's connected
        # account. No row is written, so there is nothing to resurrect
        # and nothing a revoked connection can get out of step with.
        acct = {"connected_account_id": demo_account,
                "connection_id": f"demo-{tenant}"}
    if acct is not None and ConnectorSettings.from_env().live and zoho_live_write():
        transport = transport or ComposioTransport()
        # Composio addresses the account by its own id, not our connection id.
        account = str((acct.get("connected_account_id") if isinstance(acct, dict)
                       else acct.connected_account_id)
                      or (acct.get("connection_id") if isinstance(acct, dict)
                          else acct.connection_id))
        org = os.environ.get("ZOHO_BOOKS_ORGANIZATION_ID", "").strip()
        if not org:
            # Now that writing is real, an unnamed org refuses rather than
            # guessing: the first org off the connection could be any org
            # the owner connected, and a real payment into the wrong org is
            # the worst outcome available to us.
            raise ConnectorError(
                "Zoho Books is connected but no organization was named, so the "
                "payment was not recorded anywhere.",
                next_step="Set ZOHO_BOOKS_ORGANIZATION_ID to the org id, then approve again.")
        return transport, account, org, False
    return StoreZoho(engine, tenant), "demo-zoho", "demo-org", True


def _set_payload(conn: sa.Connection, tenant: str, approval_id: str,
                 payload: dict[str, Any]) -> None:
    conn.execute(s.approvals.update().where(
        s.approvals.c.tenant_id == tenant, s.approvals.c.approval_id == approval_id)
        .values(payload=dumps(payload)))


def on_approved(engine: Engine, tenant: str, a: dict[str, Any], *,
                demo_mode: bool, transport: Any | None = None) -> None:
    """Run the code action an approved wiring approval stands for."""
    payload = dict(a.get("payload") or {})
    kind = payload.get("kind")
    now = wall()
    if kind == ZOHO_KIND:
        try:
            transport, conn_id, org, fake = _zoho(engine, tenant, demo_mode, transport)
        except ConnectorError as exc:
            # Live was asked for but the tenant has no usable org (or the
            # org read failed): nothing is recorded anywhere, and the
            # approval says so. Never fall back to demo books for a tenant
            # that asked for the real ledger.
            payload["result"] = {"error": str(exc), "undo_available": False, "demo": False}
            with engine.begin() as conn:
                _set_payload(conn, tenant, a["approval_id"], payload)
            return
        if not fake:
            # The day's real-write cap, counted per tenant since Lagos
            # midnight. Fake and demo writes never count. At the cap the
            # approval records the refusal and stays visible: nothing was
            # written, and the owner reads where to raise the limit.
            with engine.connect() as conn:
                made = zoho_live_writes_today(conn, tenant, now)
            limit = zoho_live_write_max()
            if made >= limit:
                payload["result"] = {
                    "error": ("Today's real Zoho write has already been made, "
                              "so nothing was written. To make another one, raise "
                              "MIMI_ZOHO_LIVE_WRITE_MAX to 2 and approve again."),
                    "undo_available": False, "demo": False}
                with engine.begin() as conn:
                    _set_payload(conn, tenant, a["approval_id"], payload)
                return
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
                             "demo": fake,
                             "venue": "zoho" if not fake else "mimi-book-only"}
        with engine.begin() as conn:
            _set_payload(conn, tenant, a["approval_id"], payload)
            where = ("in Zoho Books" if not fake
                     else "in Mimi's own book only (Zoho Books is not connected)")
            _say(conn, tenant, _owner_thread(conn, tenant),
                 key=f"zoho:{a['approval_id']}:done", channel="owner_console", shop_id="",
                 kind="zoho_payment_recorded", now=now,
                 text=(f"Recorded {payload['shop_name']}'s "
                       f"{owed.naira(int(payload['amount_kobo']))} payment {where}"
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
    elif kind == RETURN_CREDIT_KIND:
        # The returns ledger applies the credit itself (invoices, the credit
        # note row and the return's status), all in its own transaction.
        from mimi.api import returns as returns_ledger

        returns_ledger.apply_credit(engine, tenant, a)


def undo_approval(engine: Engine, tenant: str, approval_id: str, *,
                  demo_mode: bool, transport: Any | None = None) -> None:
    with engine.connect() as conn:
        a = conn.execute(sa.select(s.approvals).where(
            s.approvals.c.tenant_id == tenant, s.approvals.c.approval_id == approval_id)).first()
    if a is None:
        raise NotFound(approval_id)
    payload = loads(a.payload, {})
    res = payload.get("result") or {}
    if payload.get("kind") == RETURN_CREDIT_KIND:
        # The returns ledger reverses the credit (invoices back, the note row
        # marked undone, the return back to inspected).
        from mimi.api import returns as returns_ledger

        if a.status != "approved" or not res.get("undo_available"):
            raise Conflict("only an approved credit can be undone, once")
        returns_ledger.undo_credit(engine, tenant, {"approval_id": approval_id})
        return
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
    venue = str(res.get("venue") or ("mimi-book-only" if res.get("demo") else "zoho"))
    try:
        transport, conn_id, org, _fake = _zoho(engine, tenant, demo_mode, transport)
    except ConnectorError as exc:
        raise Conflict(f"Zoho did not undo it: {exc}") from exc
    try:
        zp.delete_payment(transport=transport, connection_id=conn_id, organization_id=org,
                          payment_id=str(res["zoho_payment_id"]))
    except ConnectorError as exc:
        raise Conflict(f"Zoho did not undo it: {exc}") from exc
    now = wall()
    payload["result"] = {**res, "undo_available": False, "undone_at": now_iso(now)}
    with engine.begin() as conn:
        _set_payload(conn, tenant, approval_id, payload)
        where = ("in Zoho Books" if venue == "zoho"
                 else "in Mimi's own book (it never reached Zoho Books)")
        _say(conn, tenant, _owner_thread(conn, tenant), key=f"zoho:{approval_id}:undo",
             channel="owner_console", shop_id="", kind="zoho_payment_undone", now=now,
             text=(f"Undone: {payload.get('shop_name', 'the shop')}'s payment is no longer "
                   f"{where}."))


# ------------------------------------------------------------ two books agreement


def books_agreement(engine: Engine, tenant: str,
                    transport: Any | None = None) -> dict[str, Any]:
    """Mimi's outstanding against Zoho Books', per shop and as a total.

    Sign convention, written down once: ``difference`` is Mimi minus
    Zoho. Positive means Mimi's book is higher; negative means Zoho
    reports more. A shop Mimi knows but Zoho
    does not is ``zoho_outstanding_kobo: null``, never zero: zero would
    read as "this shop owes nothing", which is a lie about money. When
    Zoho is not connected, every zoho and difference field is null and
    ``note`` says so plainly, so the console shows no figure at all.
    """
    from mimi.connectors.env import ConnectorSettings

    shops: dict[str, dict[str, Any]] = {}
    names: dict[str, str] = {}
    with engine.connect() as conn:
        for row in conn.execute(sa.select(s.shops).where(
                s.shops.c.tenant_id == tenant)):
            names[str(row.shop_id)] = str(row.name or row.shop_id)
        for row in conn.execute(sa.select(s.receivables).where(
                s.receivables.c.tenant_id == tenant,
                s.receivables.c.outstanding_kobo > 0)):
            sid = str(row.shop_id)
            shops.setdefault(sid, {"shop_id": sid, "mimi_kobo": 0})
            shops[sid]["mimi_kobo"] += int(row.outstanding_kobo)
    if not (ConnectorSettings.from_env().live and _zoho_connected(engine, tenant)):
        return _agreement(False, shops, names, {}, "Zoho Books is not connected, "
                         "so the comparison cannot be made.")
    try:
        account, org = _zoho_account_org(engine, tenant, transport)
    except ConnectorError as exc:
        # The named org is not on this token's list, or the list itself
        # refused: could-not-read, never a false agreement on nothing.
        return _agreement(False, shops, names, {},
                          f"Zoho Books could not be read ({exc}); no comparison was made.")
    try:
        zoho = _zoho_outstanding(tenant, account, org, transport)
    except ConnectorError as exc:
        return _agreement(False, shops, names, {},
                          f"Zoho Books could not be read ({exc}); no comparison was made.")
    return _agreement(True, shops, names, zoho, "")


def _zoho_connected(engine: Engine, tenant: str) -> bool:
    if _demo_wiring_on() and demo_zoho_account():
        # A demo tenant with the demo account set: connected through the
        # owner's account, with no row to write or resurrect.
        return True
    with engine.connect() as conn:
        return conn.execute(sa.select(sa.select(
            s.connector_accounts).where(
                s.connector_accounts.c.tenant_id == tenant,
                s.connector_accounts.c.toolkit == "ZOHO_BOOKS",
                s.connector_accounts.c.status == "active").exists())).scalar() or False


def _zoho_account_org(engine: Engine, tenant: str,
                      transport: Any | None = None) -> tuple[str, str]:
    """The live account and org for a read; raises rather than guessing.

    The org is pinned from the setting and then verified: the org list is
    read off the same connection, and the named org must be on it. A token
    that cannot reach the named org refuses with "could not be read" at
    the caller, never a false agreement on empty figures."""
    from mimi.connectors.zoho.books import ZohoBooksConnector

    demo_account = demo_zoho_account() if _demo_wiring_on() else ""
    with engine.connect() as conn:
        acct = conn.execute(sa.select(s.connector_accounts).where(
            s.connector_accounts.c.tenant_id == tenant,
            s.connector_accounts.c.toolkit == "ZOHO_BOOKS",
            s.connector_accounts.c.status == "active")).first()
    if acct is None and demo_account:
        account = demo_account
    elif acct is None:
        raise ConnectorError("Zoho Books is not connected.",
                             next_step="Connect it from Settings first.")
    else:
        account = str(acct.connected_account_id or acct.connection_id)
    org = os.environ.get("ZOHO_BOOKS_ORGANIZATION_ID", "").strip()
    if not org:
        raise ConnectorError("Zoho Books gave no organization.",
                             next_step="Set ZOHO_BOOKS_ORGANIZATION_ID to the org id.")
    try:
        orgs = ZohoBooksConnector(transport or ComposioTransport(), tenant_id=tenant,
                                  connection_id=account).read(
            "zoho_books_organizations").rows
    except ConnectorError as exc:
        raise ConnectorError(f"Zoho Books could not list organizations ({exc}).",
                             next_step="Reconnect Zoho Books from Settings.") from exc
    if org not in {str(r.values.get("organization_id") or "") for r in orgs}:
        raise ConnectorError(f"Zoho Books could not reach organization {org}.",
                             next_step="Connect the org that holds the books first.")
    return account, org


def _zoho_outstanding(tenant: str, account: str, org: str,
                      transport: Any | None = None) -> dict[str, int]:
    """Zoho's outstanding per Mimi shop id, matched on the ``zc-`` prefix
    Mimi writes as the Zoho customer id (``zc-<shop_id>``).

    ``org`` is the caller's organization and is unused here because the
    transport addresses the connected account directly; it is kept so the
    call reads as scoped to that org and a future multi-org tenant can
    filter on it."""
    del org
    from mimi.connectors.zoho.books import ZohoBooksConnector

    books = ZohoBooksConnector(transport or ComposioTransport(), tenant_id=tenant,
                               connection_id=account)
    out: dict[str, int] = {}
    for row in books.read("zoho_books_contacts").rows:
        cid = str(row.values.get("customer_id") or row.values.get("shop_id") or "")
        if not cid.startswith("zc-"):
            continue
        out[cid.removeprefix("zc-")] = out.get(cid.removeprefix("zc-"), 0) + int(
            row.values.get("outstanding_kobo") or 0)
    return out


def _agreement(connected: bool, shops: dict[str, dict[str, Any]],
               names: dict[str, str], zoho: dict[str, int],
               note: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    mimi_total, zoho_total = 0, 0
    for sid in sorted(set(shops) | set(zoho)):
        mimi = int(shops.get(sid, {}).get("mimi_kobo") or 0)
        zk = zoho.get(sid) if connected else None
        mimi_total += mimi
        if zk is not None:
            zoho_total += zk
        diff = (mimi - zk) if zk is not None else None
        rows.append({
            "shop_id": sid, "shop_name": names.get(sid, sid),
            "mimi_outstanding_kobo": mimi, "mimi_outstanding_naira": owed.naira(mimi),
            "zoho_outstanding_kobo": zk,
            "zoho_outstanding_naira": owed.naira(zk) if zk is not None else "",
            "difference_kobo": diff,
            "difference_naira": owed.naira(diff) if diff is not None else "",
        })
    total_zk = zoho_total if connected else None
    total_diff = (mimi_total - total_zk) if total_zk is not None else None
    return {
        "as_of": now_iso(wall()), "zoho_connected": connected, "currency": "NGN",
        "shops": rows,
        "total": {
            "mimi_outstanding_kobo": mimi_total,
            "mimi_outstanding_naira": owed.naira(mimi_total),
            "zoho_outstanding_kobo": total_zk,
            "zoho_outstanding_naira": owed.naira(total_zk) if total_zk is not None else "",
            "difference_kobo": total_diff,
            "difference_naira": owed.naira(total_diff) if total_diff is not None else "",
        },
        "note": note or ("Mimi minus Zoho: positive means Mimi's book is "
                         "higher than what Zoho Books reports."),
    }


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
