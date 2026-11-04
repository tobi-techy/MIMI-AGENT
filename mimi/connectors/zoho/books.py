"""Zoho Books connector: reads and owner-approved writes (RAI-277)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from mimi.connectors import allowlist
from mimi.connectors.base import Connector, ConnectorError
from mimi.connectors.composio import ComposioTransport, FakeComposio
from mimi.connectors.types import Proposal, ReadResult, Row, WriteRecord
from mimi.connectors.zoho import mapping as m
from mimi.connectors.zoho.replies import check_reply
from mimi.store.util import now_iso

_MAPPERS = {
    "zoho_books_organizations": m.map_organization,
    "zoho_books_contacts": m.map_contact,
    "zoho_books_contact": m.map_contact,
    "zoho_books_invoices": m.map_invoice,
    "zoho_books_invoice": m.map_invoice,
    "zoho_books_customer_payments": m.map_customer_payment,
    "zoho_books_invoice_payments": m.map_customer_payment,
    "zoho_books_items": m.map_item,
}

_WRITES = {
    "zoho_books_invoice_comment",
    "zoho_books_contact_update",
    "zoho_books_customer_payment",
}


class ZohoBooksConnector(Connector):
    """Zoho Books behind the Composio transport. Reads are direct; writes
    only as owner-approved proposals, idempotent, audited, reversible."""

    name = "zoho_books"

    def __init__(self, transport: ComposioTransport | FakeComposio,
                 *, tenant_id: str, connection_id: str) -> None:
        self.transport = transport
        self.tenant_id = tenant_id
        self.connection_id = connection_id

    def read(self, resource: str, filters: dict[str, Any] | None = None) -> ReadResult:
        mapper = _MAPPERS.get(resource)
        row = allowlist.get(resource)
        if mapper is None or row is None or row.kind != "read":
            raise ConnectorError(f"{resource} is not a wrapped Zoho Books read.",
                                 next_step="Use one of the wrapped reads.")
        raw = self.transport.execute(resource, user_id=self.tenant_id,
                                     connected_account_id=self.connection_id,
                                     arguments=dict(filters or {}))
        raw = check_reply(resource, raw)
        items = _items(raw)
        rows = [Row(values=mapper(i), source=resource) for i in items]
        if resource == "zoho_books_organizations":
            orgs = raw.get("organizations")
            if isinstance(orgs, list):
                rows = [Row(values=mapper(i), source=resource)
                        for i in orgs if isinstance(i, dict)]
        return ReadResult(rows=rows, rows_read=len(rows),
                          rows_mapped=sum(1 for r in rows if r.values.get("shop_id")
                                          or r.values.get("sku")
                                          or r.values.get("delivery_id")),
                          errors=[])

    def propose_write(self, action: str, payload: dict[str, Any]) -> Proposal:
        if action not in _WRITES:
            raise ConnectorError(f"{action} is not a wrapped Zoho Books write.",
                                 next_step="Use an invoice comment, a contact "
                                           "update or a customer payment.")
        row = allowlist.get(action)
        assert row is not None
        before, after = _before_after(action, payload)
        key_src = f"{self.tenant_id}:{action}:{_stable(payload)}"
        key = f"zoho-books:{hashlib.sha256(key_src.encode()).hexdigest()[:24]}"
        amount = payload.get("amount_kobo")
        return Proposal(
            proposal_id=f"prop-{key}", action=action, tenant_id=self.tenant_id,
            connection_id=self.connection_id, before=before, after=after,
            amount_kobo=int(amount) if isinstance(amount, int) else None,
            idempotency_key=key, preview=_preview(action, payload))

    def apply_write(self, proposal: Proposal) -> WriteRecord:
        if not proposal.approval_id:
            raise ConnectorError("That write has no approval yet.",
                                 next_step="Wait for the owner to approve it.")
        arguments = _arguments(proposal.action, proposal.after)
        reply = self.transport.execute_write(
            proposal.action, user_id=self.tenant_id,
            connected_account_id=self.connection_id, arguments=arguments)
        external = str(reply.get("comment_id") or reply.get("contact_id")
                       or reply.get("payment_id") or "")
        payload_hash = hashlib.sha256(_stable(arguments).encode()).hexdigest()
        return WriteRecord(
            write_id=f"wr-{proposal.idempotency_key}", proposal_id=proposal.proposal_id,
            action=proposal.action, tenant_id=self.tenant_id,
            connection_id=self.connection_id, idempotency_key=proposal.idempotency_key,
            payload_hash=payload_hash, before=dict(proposal.before),
            after=dict(proposal.after), external_id=external,
            approval_id=proposal.approval_id, applied_at=now_iso())

    def undo(self, write: WriteRecord) -> WriteRecord:
        row = allowlist.get(write.action)
        if row is None or not row.undo_action:
            raise ConnectorError(f"{write.action} has no undo.",
                                 next_step="Fix it by hand in Zoho Books.")
        undo_args = _undo_arguments(write)
        self.transport.execute_write(
            row.undo_action, user_id=self.tenant_id,
            connected_account_id=self.connection_id, arguments=undo_args)
        out = write.model_copy(deep=True)
        out.undone_at = now_iso()
        return out


def _items(raw: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("contacts", "invoices", "customer_payments", "invoice_payments",
                "items", "data", "records"):
        val = raw.get(key)
        if isinstance(val, list):
            return [v for v in val if isinstance(v, dict)]
    if raw and all(isinstance(v, dict) for v in raw.values()):
        return [dict(v) for v in raw.values()]
    single = raw.get("contact") or raw.get("invoice") or raw.get("item")
    return [single] if isinstance(single, dict) else []


def _stable(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _before_after(action: str, payload: dict[str, Any]) -> tuple[dict, dict]:
    if action == "zoho_books_contact_update":
        split = m.contact_update_payload(dict(payload.get("current") or {}),
                                         dict(payload.get("changes") or {}))
        return split["before"], split["after"]
    if action == "zoho_books_invoice_comment":
        return {}, {"invoice_id": payload.get("invoice_id"),
                    "comment": payload.get("comment")}
    # Customer payment: the before state is "no payment with this
    # reference"; the after state is the payment to record.
    return {"exists": False}, {
        "customer_id": payload.get("customer_id"),
        "amount_kobo": payload.get("amount_kobo"),
        "date": payload.get("date"),
        "reference_number": payload.get("reference_number"),
        "invoices": payload.get("invoices") or [],
        "payment_mode": payload.get("payment_mode") or "Bank Transfer"}


def _preview(action: str, payload: dict[str, Any]) -> str:
    if action == "zoho_books_invoice_comment":
        return "Add a payment note on the Zoho invoice."
    if action == "zoho_books_contact_update":
        fields = ", ".join(sorted((payload.get("changes") or {}).keys()))
        return f"Update the Zoho contact ({fields})."
    from mimi.connectors.zoho.payments import preview_payment

    return preview_payment(payload)


def _arguments(action: str, after: dict[str, Any]) -> dict[str, Any]:
    if action == "zoho_books_contact_update":
        return dict(after)
    if action == "zoho_books_invoice_comment":
        return {"invoice_id": after.get("invoice_id"), "comment": after.get("comment")}
    return dict(after)


def _undo_arguments(write: WriteRecord) -> dict[str, Any]:
    if write.action == "zoho_books_contact_update":
        return dict(write.before)
    if write.action == "zoho_books_invoice_comment":
        return {"comment_id": write.external_id}
    return {"payment_id": write.external_id}
