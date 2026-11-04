"""What a payment does when it is larger than the shop owes.

A payment bigger than the open balance used to leave its excess nowhere. The
two payment writers clamped the receivable (``demo_wiring._apply_to_invoices``
and ``week.go.GoSim._pay``) while the ``payments`` row kept the full amount the
bank sent, so the two tables disagreed by the excess and nothing complained. And
the Zoho card built from that payment proposed the whole amount against one
invoice, which in a real Zoho Books org is an over-application: the shop's record
says it settled more of an invoice than the invoice ever owed.

Both halves are fixed here rather than in either writer, so the demo's bank
credit and the simulated week behave the same:

* the amount applied to each invoice is capped at that invoice's open balance,
  and the card says so (``payments.create_body`` takes an invoice list that adds
  up to the payment or to less than it, never to more: what is left unapplied is
  a credit on the customer's account in Zoho);
* the excess is recorded on the shop's account as a ``credit_notes`` row, which
  already carries ``on_account_kobo`` from 0017. No new table, no new revision: a
  payment credit has no return, so ``return_id`` is empty and its ``credit_id``
  is built from the payment id. ``for_shop_kobo`` is what the shop holds on
  account (every credit note on it that has not been undone, returns included).

Allocating that credit onto a later invoice is deliberately not built: it wants
its own approval and its own undo, and tonight it only has to be visible and to
add up.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa

from mimi.clock import wall
from mimi.store import schema as s
from mimi.store.util import dumps, insert_ignore, now_iso


def credit_id_for(tenant_id: str, payment_id: str) -> str:
    """The credit note id for a payment's on-account part.

    The same shape as a return credit (``crn_`` plus a hash), so the scrub that
    keeps internal ids out of anything a person reads catches this one too."""
    return "crn_" + hashlib.sha256(
        f"{tenant_id}\x00payment\x00{payment_id}".encode()).hexdigest()[:20]


def amount_kobo(amount: int, applied_kobo: int) -> int:
    """What is left on the shop's account when a payment is taken off its
    invoices: the payment less what the invoices could absorb, never negative."""
    return max(0, int(amount) - max(0, int(applied_kobo)))


def record(conn: sa.Connection, *, tenant: str, shop_id: str, payment_id: str,
           amount: int, applied_kobo: int, applications: Sequence[Mapping[str, Any]],
           approval_id: str = "", now: float | None = None) -> str:
    """Record a payment's on-account part, and nothing when there is none.

    Returns the credit note id, or an empty string when the payment came off
    the invoices in full. Idempotent on the payment: the id is derived from it,
    so a repeated call writes the one row."""
    on_account = amount_kobo(amount, applied_kobo)
    if on_account <= 0:
        return ""
    credit_id = credit_id_for(tenant, payment_id)
    insert_ignore(conn, s.credit_notes, {
        "tenant_id": tenant, "credit_id": credit_id, "return_id": "", "shop_id": shop_id,
        "approval_id": approval_id, "amount_kobo": int(amount),
        "applied_kobo": int(applied_kobo), "on_account_kobo": on_account,
        "applications": dumps(list(applications)),
        "applied_at": now_iso(wall() if now is None else now)})
    return credit_id


def for_shop_kobo(conn: sa.Connection, tenant: str, shop_id: str) -> int:
    """What the shop holds on account: every credit note on it not undone.

    Returns credits and payment over-payments are the same money here, which is
    what the shop's statement should say: the distributor owes the shop that
    much, and no invoice has it yet."""
    return int(conn.execute(sa.select(sa.func.coalesce(
        sa.func.sum(s.credit_notes.c.on_account_kobo), 0)).where(
        s.credit_notes.c.tenant_id == tenant,
        s.credit_notes.c.shop_id == shop_id,
        s.credit_notes.c.undone_at.is_(None))).scalar() or 0)
