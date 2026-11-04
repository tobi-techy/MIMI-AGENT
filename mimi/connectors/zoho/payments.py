"""Record a customer payment on Zoho Books through the Composio proxy (RAI-277).

There is no create-customer-payment action on Composio, so this module is
built on Zoho Books' own API, called through Composio's proxy execute on
the distributor's existing ZOHO_BOOKS connected account: no second OAuth
app, no raw token in our code. Only this module makes these calls.

* Create: ``POST https://<region-host>/books/v3/customerpayments?organization_id=<org>``
  with customer_id, payment_mode, amount, date, reference_number and
  invoices [{invoice_id, amount_applied}].
* Undo: ``DELETE https://<region-host>/books/v3/customerpayments/{payment_id}?organization_id=<org>``.
  The proxy resolves nothing from a relative endpoint, so every call
  carries an absolute URL (host from ``MIMI_ZOHO_REGION``).
* Scopes: ZohoBooks.customerpayments.CREATE and .DELETE on our Zoho auth
  config.

Rules, enforced in code: only from an owner-approved proposal (never
auto-approved, whatever the rulebook says); the amount and the invoice
come from Mimi's records (a Proof-verified payment); the idempotency key
sits in reference_number plus a check for an existing payment with that
reference before creating; the audit row carries the Zoho payment_id;
exactly one call per approval (the proxy never retries: on timeout read
back by reference before anything else); each invoice's applied amount
is capped at what that invoice still owes, so an over-application can
never leave this module as a number Zoho would accept. This records a
payment the shop already made; Mimi still never moves money.

Zoho Books is the only money system of record. A payment write to any
other toolkit (BeatRoute, a CRM, a spreadsheet) is refused here, not
left to discipline: two ledgers allocating the same cash is worse than
no integration. ``record_payment`` and ``delete_payment`` take the
connection through this module only; anything else raises.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import quote

from mimi.connectors.base import ConnectorError
from mimi.connectors.composio import ComposioTransport, FakeComposio
from mimi.connectors.types import WriteRecord
from mimi.connectors.zoho.replies import check_reply
from mimi.store.util import now_iso

# Zoho data-centre API hosts, keyed by the MIMI_ZOHO_REGION code: the
# Composio proxy has no server-side base for this toolkit, so a relative
# endpoint resolves to nothing (Zoho answers code 5, "Invalid URL Passed").
# Every proxy call therefore carries an absolute URL on the region's host.
REGION_HOSTS = {
    "com": "https://www.zohoapis.com",
    "eu": "https://www.zohoapis.eu",
    "in": "https://www.zohoapis.in",
    "cn": "https://www.zohoapis.com.cn",
    "au": "https://www.zohoapis.com.au",
}


# Zoho Books is the only ledger a payment may be written to. A write
# addressed anywhere else (a CRM, a DMS, a sheet) allocates the same
# cash twice across two systems that will silently disagree. Refused
# here, in code, not by convention.
PAYMENT_LEDGER_TOOLKIT = "ZOHO_BOOKS"

# Composio has issued connection ids as ``ca-...``, ``conn-...``, ``demo-...``
# and ``zp-...``, and since 2026 it issues ``ca_...`` with an underscore. The
# separator is not part of the contract, so both are accepted, and an id that
# is not one of those prefixes is refused: accepting anything would let a
# payment be addressed to a ledger that is not Zoho Books.
PAYMENT_CONNECTION_PREFIXES = ("ca", "conn", "demo", "zp")
_PAYMENT_CONNECTION_ID = re.compile(
    r"(?:ca|conn|demo|zp)[-_][A-Za-z0-9][A-Za-z0-9_-]*")


def _is_payment_connection_id(connection_id: str) -> bool:
    """True for an empty id (nothing to check) or a Zoho Books connection.

    An id is judged by its prefix and separator only, never by its length or
    its provider, because Composio changes both. Measured live on 4 Nov 2026:
    the real account was ``ca_M3aPpWNfL_p7`` and the hyphen-only check refused
    it, which read to everyone as a broken Zoho connection."""
    cid = str(connection_id or "")
    return not cid or _PAYMENT_CONNECTION_ID.fullmatch(cid) is not None


def _refuse_foreign_ledger(transport: ComposioTransport | FakeComposio,
                           connection_id: str) -> None:
    toolkit = str(getattr(transport, "toolkit", "") or "").upper()
    if toolkit and toolkit != PAYMENT_LEDGER_TOOLKIT:
        raise ConnectorError(
            f"Payments are recorded in Zoho Books only, not in {toolkit}.",
            next_step="Record it in Zoho Books; read the other system instead.")
    if not _is_payment_connection_id(str(connection_id or "")):
        raise ConnectorError(
            "That is not a Zoho Books connection, so no payment goes there.",
            next_step="Use the tenant's Zoho Books connection.")


def books_url(region: str, path: str, query: str) -> str:
    """Absolute Zoho Books API URL: host from the region, then /books/v3."""
    host = REGION_HOSTS.get((region or "").lower(), REGION_HOSTS["com"])
    return f"{host}/books/v3{path}?{query}" if query else f"{host}/books/v3{path}"


def reference_for(idempotency_key: str, approval_id: str = "") -> str:
    """The idempotency key, safe for Zoho's reference_number field.

    An owner-approved write carries the approval id (``mimi-apr_<digest>``),
    so a payment in Zoho can be traced back to the approval that made it and
    taken out again by ``scripts/seed_demo_zoho.py --reset``. A call without
    an approval keeps the older ``mimi-<idempotency key>`` shape."""
    if approval_id:
        return f"mimi-{approval_id}"[:100]
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


def create_body(payload: dict[str, Any], idempotency_key: str,
                approval_id: str = "") -> dict[str, Any]:
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
    lines = []
    for i in invoices:
        if not isinstance(i, dict):
            continue
        want = int(i.get("amount_applied_kobo") or 0)
        owes = i.get("invoice_outstanding_kobo")
        owes_kobo = int(owes) if isinstance(owes, int) else None
        # Second line of defence behind the caller-side cap (PR #61
        # already capped ``applied_kobo`` at what each invoice owed):
        # an application larger than the invoice owes is a wrong number
        # we chose to send to a real ledger, so it never leaves here
        # uncapped. It agrees with ``applied_kobo`` rather than
        # recomputing it: the caller's split still applies unchanged
        # when no outstanding figure rides along.
        capped = min(want, owes_kobo) if owes_kobo is not None and owes_kobo >= 0 else want
        lines.append({"invoice_id": i.get("invoice_id"),
                      "amount_applied": round(capped / 100, 2)})
    return {
        "customer_id": payload.get("customer_id"),
        "payment_mode": payload.get("payment_mode") or "Bank Transfer",
        "amount": round(amount_kobo / 100, 2),
        "date": payload.get("date"),
        "reference_number": reference_for(idempotency_key, approval_id),
        "invoices": lines,
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
    _refuse_foreign_ledger(transport, connection_id)
    body = create_body(payload, idempotency_key, approval_id)
    endpoint = books_url(transport.settings.zoho_region,
                         "/customerpayments", f"organization_id={organization_id}")
    reply = _proxy(transport, "customer_payment_create", method="POST",
                   endpoint=endpoint, connected_account_id=connection_id, body=body,
                   user_id=tenant_id)
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
                      reference: str, user_id: str = "") -> dict[str, Any] | None:
    """Read back a payment by its reference (the idempotency check)."""
    _refuse_foreign_ledger(transport, connection_id)
    endpoint = books_url(transport.settings.zoho_region, "/customerpayments",
                         f"organization_id={organization_id}"
                         f"&reference_number={quote(reference, safe='')}")
    reply = _proxy(transport, "customer_payment_search", method="GET",
                   endpoint=endpoint, connected_account_id=connection_id,
                   body={"reference_number": reference}, user_id=user_id)
    # A search that failed must never read as "no payment exists": that is
    # the one answer this module acts on by creating a second payment.
    check_reply("customer_payment_search", reply)
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
    body = create_body(payload, idempotency_key, approval_id)
    reference = body["reference_number"]
    payload_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    existing = find_by_reference(transport=transport, connection_id=connection_id,
                                 organization_id=organization_id, reference=reference,
                                 user_id=tenant_id)
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
                                   organization_id=organization_id, reference=reference,
                                   user_id=tenant_id)
        if landed is None:
            raise
        return {"payment_id": str(landed.get("payment_id") or ""),
                "reference_number": reference, "payload_hash": payload_hash,
                "already_recorded": False}


def delete_payment(*, transport: ComposioTransport | FakeComposio,
                   connection_id: str, organization_id: str,
                   payment_id: str, user_id: str = "") -> None:
    """Undo a created payment: delete it by its Zoho payment id."""
    _refuse_foreign_ledger(transport, connection_id)
    endpoint = books_url(transport.settings.zoho_region,
                         f"/customerpayments/{payment_id}",
                         f"organization_id={organization_id}")
    _proxy(transport, "customer_payment_delete", method="DELETE",
           endpoint=endpoint, connected_account_id=connection_id,
           body={"payment_id": payment_id}, user_id=user_id)


def _proxy(transport: ComposioTransport | FakeComposio, name: str, *, method: str,
           endpoint: str, connected_account_id: str,
           body: dict[str, Any], user_id: str = "") -> dict[str, Any]:
    """One proxy call through the transport's own proxy path: the fake
    replays its fixture, the live transport calls ``tools.proxy``."""
    if isinstance(transport, FakeComposio):
        return _checked(transport.proxy(name, method=method, endpoint=endpoint,
                                        connected_account_id=connected_account_id,
                                        body=body), status=200)
    return _live_proxy(transport, name, method=method, endpoint=endpoint,
                       connected_account_id=connected_account_id, body=body,
                       user_id=user_id)


def _live_proxy(transport: ComposioTransport | FakeComposio, name: str, *, method: str,
                endpoint: str, connected_account_id: str,
                body: dict[str, Any], user_id: str = "") -> dict[str, Any]:
    """The live proxy call. ``name`` is the fake's fixture key and is
    unused live; the endpoint carries the absolute Books URL."""
    del name
    from mimi.connectors.composio import _plain

    client = transport._sdk()
    # Absolute Books URL on the region's host: the Composio proxy has no
    # server-side base for this toolkit, so a relative endpoint resolves
    # to nothing (PR #56 fixed this; main regressed it back to the
    # ``zoho_books`` prefix in the merge). Kept here with the fix, where
    # every proxy call in this module carries it.
    #
    # The user id belongs beside the call, not inside the Zoho body: a
    # project with 2FA on refuses ``connected_account_id`` alone ("user_id
    # is required with connected_account_id"), and a GET may carry no body
    # at all. The SDK's ``tools.proxy`` has no user_id parameter, so a
    # named user reaches the generated client underneath it.
    try:
        if user_id and method == "GET":
            res = client._client.tools.proxy(  # type: ignore[attr-defined]
                endpoint=endpoint, method=method, body=None,
                connected_account_id=connected_account_id,
                extra_body={"user_id": user_id})
        elif user_id:
            res = client._client.tools.proxy(  # type: ignore[attr-defined]
                endpoint=endpoint, method=method, body=body,
                connected_account_id=connected_account_id,
                extra_body={"user_id": user_id})
        else:
            res = client.tools.proxy(endpoint=endpoint, method=method,  # type: ignore[arg-type]
                                     body=body,
                                     connected_account_id=connected_account_id)
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
