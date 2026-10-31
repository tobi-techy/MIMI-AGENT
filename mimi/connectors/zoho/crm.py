"""Zoho CRM connector: account and contact reads, owner-approved notes (RAI-277)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from mimi.connectors import allowlist
from mimi.connectors.base import Connector, ConnectorError
from mimi.connectors.composio import ComposioTransport, FakeComposio
from mimi.connectors.types import Proposal, ReadResult, Row, WriteRecord
from mimi.connectors.zoho import mapping as m
from mimi.store.util import now_iso

_MAPPERS = {
    "zoho_crm_accounts": m.map_crm_account,
    "zoho_crm_accounts_search": m.map_crm_account,
    "zoho_crm_contacts": m.map_crm_account,
    "zoho_crm_contacts_search": m.map_crm_account,
    "zoho_crm_record": m.map_crm_account,
}


class ZohoCrmConnector(Connector):
    """Zoho CRM behind the Composio transport. Writes are notes only, as
    owner-approved proposals with an undo (delete the note)."""

    name = "zoho_crm"

    def __init__(self, transport: ComposioTransport | FakeComposio,
                 *, tenant_id: str, connection_id: str) -> None:
        self.transport = transport
        self.tenant_id = tenant_id
        self.connection_id = connection_id

    def read(self, resource: str, filters: dict[str, Any] | None = None) -> ReadResult:
        mapper = _MAPPERS.get(resource)
        row = allowlist.get(resource)
        if mapper is None or row is None or row.kind != "read":
            raise ConnectorError(f"{resource} is not a wrapped Zoho CRM read.",
                                 next_step="Use one of the wrapped reads.")
        raw = self.transport.execute(resource, user_id=self.tenant_id,
                                     connected_account_id=self.connection_id,
                                     arguments=dict(filters or {}))
        data = raw.get("data")
        recs = raw.get("records", [])
        raw_items = data if isinstance(data, list) else recs if isinstance(recs, list) else []
        items = [i for i in raw_items if isinstance(i, dict)]
        rows = [Row(values=mapper(i), source=resource) for i in items]
        return ReadResult(rows=rows, rows_read=len(items),
                          rows_mapped=sum(1 for r in rows if r.values.get("shop_id")),
                          errors=[])

    def propose_write(self, action: str, payload: dict[str, Any]) -> Proposal:
        if action != "zoho_crm_note":
            raise ConnectorError(f"{action} is not a wrapped Zoho CRM write.",
                                 next_step="Notes are the only CRM write.")
        key_src = f"{self.tenant_id}:{action}:{json.dumps(payload, sort_keys=True, default=str)}"
        key = f"zoho-crm:{hashlib.sha256(key_src.encode()).hexdigest()[:24]}"
        return Proposal(
            proposal_id=f"prop-{key}", action=action, tenant_id=self.tenant_id,
            connection_id=self.connection_id, before={},
            after={"record_id": payload.get("record_id"),
                   "module": payload.get("module") or "Accounts",
                   "note": payload.get("note")},
            idempotency_key=key, preview="Add a note on the Zoho CRM record.")

    def apply_write(self, proposal: Proposal) -> WriteRecord:
        if not proposal.approval_id:
            raise ConnectorError("That note has no approval yet.",
                                 next_step="Wait for the owner to approve it.")
        reply = self.transport.execute_write(
            proposal.action, user_id=self.tenant_id,
            connected_account_id=self.connection_id, arguments=dict(proposal.after))
        external = str(reply.get("id") or reply.get("note_id") or "")
        payload_hash = hashlib.sha256(json.dumps(
            proposal.after, sort_keys=True, default=str).encode()).hexdigest()
        return WriteRecord(
            write_id=f"wr-{proposal.idempotency_key}", proposal_id=proposal.proposal_id,
            action=proposal.action, tenant_id=self.tenant_id,
            connection_id=self.connection_id, idempotency_key=proposal.idempotency_key,
            payload_hash=payload_hash, before={}, after=dict(proposal.after),
            external_id=external, approval_id=proposal.approval_id, applied_at=now_iso())

    def undo(self, write: WriteRecord) -> WriteRecord:
        row = allowlist.get(write.action)
        if row is None or not row.undo_action:
            raise ConnectorError(f"{write.action} has no undo.",
                                 next_step="Delete the note by hand in Zoho CRM.")
        self.transport.execute_write(
            row.undo_action, user_id=self.tenant_id,
            connected_account_id=self.connection_id,
            arguments={"note_id": write.external_id})
        out = write.model_copy(deep=True)
        out.undone_at = now_iso()
        return out
