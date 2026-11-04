"""Record a customer payment on Zoho Books through the Composio proxy (RAI-277).

There is no create-customer-payment action on Composio, so this module is
built on Zoho Books' own API, called through Composio's proxy execute on
the distributor's existing ZOHO_BOOKS connected account: no second OAuth
app, no raw token in our code. Only this module makes these calls.

* Create: ``POST /customerpayments?organization_id=<org>`` with
  customer_id, payment_mode, amount, date, reference_number and
  invoices [{invoice_id, amount_applied}].
* Undo: ``DELETE /customerpayments/{payment_id}?organization_id=<org>``.
* Scopes: ZohoBooks.customerpayments.CREATE and .DELETE on our Zoho auth
  config.

Rules, enforced in code: only from an owner-approved proposal (never
auto-approved, whatever the rulebook says); the amount and the invoice
come from Mimi's records (a Proof-verified payment); the idempotency key
sits in reference_number plus a check for an existing payment with that
reference before creating; the audit row carries the Zoho payment_id;
exactly one call per approval (the proxy never retries: on timeout read
back by reference before anything else). This records a payment the shop
already made; Mimi still never moves money.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import quote

from mimi.connectors.base import ConnectorError
from mimi.connectors.composio import ComposioTransport, FakeComposio
from mimi.connectors.types import WriteRecord
from mimi.store.util import now_iso


def reference_for(idempotency_key: str) -> str:
    """The idempotency key, safe for Zoho's reference_number field."""
    return f"mimi-{idempotency_key}"[:100]


def preview_payment(payload: dict[str, Any]) -> str:
    amount = payload.get("amount_kobo")
    if isinstance(amount, int):
        text = f"\u20a6{amount / 100:,.0f}" if amount % 100 == 0 else f"\u20a6{amount / 100:,.2f}"
    else:
        text = "the amount"
    shop = payload.get("shop_name") or payload.get("customer_id") or "the shop"
    on_account = payload.get("on_account_kobo")
    waiting = payload.get("waiting_for_roll_kobo")
    billed = payload.get("billed_by_roll_kobo")
    held = (_naira(int(on_account)) if isinstance(on_account, int) and on_account > 0 else "")
    later = (_naira(int(waiting)) if isinstance(waiting, int) and waiting > 0 else "")
    already = (_naira(int(billed)) if isinstance(billed, int) and billed > 0 else "")
    said = []
    if already:
        said.append(f"{already} of it is already off that delivery's invoice in the book")
    if later:
        said.append(f"{later} of it goes on the delivery it was sent for once that "
                    "delivery is billed")
    if held:
        said.append(f"{held} of it stays on the shop's account: it owed less than that")
    if said:
        return (f"Record {shop}'s {text} transfer in Zoho Books. "
                + ", and ".join(said) + ".")
    return f"Record {shop}'s {text} transfer in Zoho Books."


def _naira(kobo: int) -> str:
    whole, part = divmod(int(kobo), 100)
    return f"\u20a6{whole:,}" + (f".{part:02d}" if part else "")


def create_body(payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
    """The Zoho create-customer-payment body. Amounts leave here in naira,
    computed from integer kobo in code.

    The invoice list may add up to the payment or to less than it, never to
    more: less means part of the payment was not applied to any invoice, which
    Zoho Books holds as an unapplied amount on the customer payment. That is the
    case when a shop paid more than it owed, and the alternative (applying it to
    an invoice anyway) would record a bigger application than the invoice ever
    owed."""
    amount_kobo = payload.get("amount_kobo")
    if not isinstance(amount_kobo, int) or amount_kobo <= 0:
        raise ConnectorError("A customer payment needs a positive amount in kobo.",
                             next_step="Price it from Mimi's records first.")
    if not payload.get("customer_id"):
        raise ConnectorError("A customer payment needs the Zoho customer id.",
                             next_step="Match the shop to its Zoho contact first.")
    if not payload.get("date"):
        raise ConnectorError("A customer payment needs the payment date.",
                             next_step="Take the date from the verified payment.")
    invoices = payload.get("invoices") or []
    applied = sum(int(i.get("amount_applied_kobo") or 0) for i in invoices
                  if isinstance(i, dict))
    if applied > amount_kobo:
        raise ConnectorError(
            "The invoice amounts add up to more than the payment.",
            next_step="Cap each application at the invoice's open balance in code first.")
    return {
        "customer_id": payload.get("customer_id"),
        "payment_mode": payload.get("payment_mode") or "Bank Transfer",
        "amount": round(amount_kobo / 100, 2),
        "date": payload.get("date"),
        "reference_number": reference_for(idempotency_key),
        "invoices": [{"invoice_id": i.get("invoice_id"),
                      "amount_applied": round(int(i.get("amount_applied_kobo") or 0) / 100, 2)}
                     for i in invoices if isinstance(i, dict)],
    }


def create_payment(*, transport: ComposioTransport | FakeComposio, tenant_id: str,
                   connection_id: str, organization_id: str, payload: dict[str, Any],
                   idempotency_key: str,
                   approval_id: str = "") -> dict[str, Any]:
    """Create one customer payment. Exactly one proxy call: the proxy never
    retries, and on timeout the caller reads back by reference first."""
    if not approval_id:
        raise ConnectorError("A customer payment needs the owner's approval.",
                             next_step="Wait for the owner to approve it.")
    body = create_body(payload, idempotency_key)
    endpoint = f"/customerpayments?organization_id={organization_id}"
    reply = _proxy(transport, "customer_payment_create", method="POST",
                   endpoint=endpoint, connected_account_id=connection_id, body=body)
    # Zoho Books answers a create with ``{"code": 0, "payment": {...}}``.
    payment = reply.get("payment") or reply.get("customerpayment") or reply
    if not isinstance(payment, dict):
        payment = {}
    payment_id = str(payment.get("payment_id") or "")
    if not payment_id:
        raise ConnectorError("Zoho gave no payment id back.",
                             next_step="Read back by reference before retrying.")
    return {"payment_id": payment_id, "reference_number": body["reference_number"],
            "payload_hash": hashlib.sha256(
                json.dumps(body, sort_keys=True).encode()).hexdigest()}


def find_by_reference(*, transport: ComposioTransport | FakeComposio,
                      connection_id: str, organization_id: str,
                      reference: str) -> dict[str, Any] | None:
    """Read back a payment by its reference (the idempotency check)."""
    endpoint = (f"/customerpayments?organization_id={organization_id}"
                f"&reference_number={quote(reference, safe='')}")
    reply = _proxy(transport, "customer_payment_search", method="GET",
                   endpoint=endpoint, connected_account_id=connection_id,
                   body={"reference_number": reference})
    payments = [p for p in (reply.get("customer_payments") or [])
                if isinstance(p, dict) and p.get("reference_number") == reference]
    return dict(payments[0]) if payments else None


def record_payment(*, transport: ComposioTransport | FakeComposio, tenant_id: str,
                   connection_id: str, organization_id: str, payload: dict[str, Any],
                   idempotency_key: str, approval_id: str = "") -> dict[str, Any]:
    """The one way to record an owner-approved payment: read back by
    reference first (an earlier attempt may have landed), then exactly one
    create; when the create fails or times out, read back again before
    anything else. Never a second create in the same call."""
    if not approval_id:
        raise ConnectorError("A customer payment needs the owner's approval.",
                             next_step="Wait for the owner to approve it.")
    body = create_body(payload, idempotency_key)
    reference = body["reference_number"]
    payload_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    existing = find_by_reference(transport=transport, connection_id=connection_id,
                                 organization_id=organization_id, reference=reference)
    if existing is not None:
        return {"payment_id": str(existing.get("payment_id") or ""),
                "reference_number": reference, "payload_hash": payload_hash,
                "already_recorded": True}
    try:
        return {**create_payment(transport=transport, tenant_id=tenant_id,
                                 connection_id=connection_id,
                                 organization_id=organization_id, payload=payload,
                                 idempotency_key=idempotency_key,
                                 approval_id=approval_id),
                "already_recorded": False}
    except ConnectorError:
        landed = find_by_reference(transport=transport, connection_id=connection_id,
                                   organization_id=organization_id, reference=reference)
        if landed is None:
            raise
        return {"payment_id": str(landed.get("payment_id") or ""),
                "reference_number": reference, "payload_hash": payload_hash,
                "already_recorded": False}


def delete_payment(*, transport: ComposioTransport | FakeComposio,
                   connection_id: str, organization_id: str,
                   payment_id: str) -> None:
    """Undo a created payment: delete it by its Zoho payment id."""
    endpoint = f"/customerpayments/{payment_id}?organization_id={organization_id}"
    _proxy(transport, "customer_payment_delete", method="DELETE",
           endpoint=endpoint, connected_account_id=connection_id,
           body={"payment_id": payment_id})


def _proxy(transport: ComposioTransport | FakeComposio, name: str, *, method: str,
           endpoint: str, connected_account_id: str,
           body: dict[str, Any]) -> dict[str, Any]:
    if isinstance(transport, FakeComposio):
        return _checked(transport.proxy(name, method=method, endpoint=endpoint,
                                        connected_account_id=connected_account_id,
                                        body=body), status=200)
    from mimi.connectors.composio import _plain

    client = transport._sdk()
    try:
        res = client.tools.proxy(endpoint=f"zoho_books{endpoint}", method=method,  # type: ignore[arg-type]
                                 body=body, connected_account_id=connected_account_id)
    except Exception as exc:
        raise ConnectorError(f"The Zoho Books payment call failed: {exc}",
                             next_step="Read back by reference before retrying.") from exc
    status = getattr(res, "status", None)
    return _checked(_plain(res), status=int(status) if isinstance(status, int | float) else 200)


def _checked(reply: dict[str, Any], *, status: int) -> dict[str, Any]:
    """Refuse an HTTP error or a Zoho error code: a failed create or delete
    must never read as done."""
    code = reply.get("code", 0)
    if status >= 400 or (isinstance(code, int) and code != 0):
        raise ConnectorError(
            f"Zoho Books refused the payment call (status {status}, code {code}).",
            next_step="Read back by reference before retrying.")
    return reply


def undo_record(write: WriteRecord) -> WriteRecord:
    out = write.model_copy(deep=True)
    out.undone_at = now_iso()
    return out
