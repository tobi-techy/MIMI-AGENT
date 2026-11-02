"""Typed proposals and write records for every connector (RAI-277).

A ``Proposal`` is what the owner sees in approvals: what changes, from
what to what, with an idempotency key so applying twice is one write.
A ``WriteRecord`` is the audit row after it applied: the external id the
outside tool gave back, the approval that allowed it, and when it was
undone, if it ever was. Money is integer kobo, read from the book or
summed in code, never written by the model.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Proposal(_Strict):
    """One outside write, proposed, waiting for the owner's approval."""

    proposal_id: str = Field(description="Mimi's id for this proposal.")
    action: str = Field(description="Mimi's action name (allowlist.py).")
    tenant_id: str = Field(description="The distributor this write belongs to.")
    connection_id: str = Field(description="The connector account to write through.")
    # What the outside record looked like before, and what it should look
    # like after. Both plain JSON dicts, read or computed in code.
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    # Integer kobo when the write names money (a payment note's amount);
    # None when it names none (a contact note). Never from the model.
    amount_kobo: int | None = Field(default=None)
    # The business key: applying the same proposal twice is one write.
    idempotency_key: str = Field(description="Stable key for this exact write.")
    # The approval that allowed it; empty until the owner approves.
    approval_id: str = Field(default="")
    preview: str = Field(default="", description="Plain words for the owner.")


class WriteRecord(_Strict):
    """The audit row for an applied (or undone) outside write."""

    write_id: str = Field(description="Mimi's id for this applied write.")
    proposal_id: str = Field(description="The proposal this write applied.")
    action: str = Field(description="Mimi's action name (allowlist.py).")
    tenant_id: str = Field(description="The distributor this write belongs to.")
    connection_id: str = Field(description="The connector account written through.")
    idempotency_key: str = Field(description="The proposal's business key.")
    payload_hash: str = Field(description="SHA-256 of the applied payload.")
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    # The outside tool's id (a Zoho payment id): the handle for undo.
    external_id: str = Field(default="")
    approval_id: str = Field(default="", description="The approval that allowed it.")
    applied_at: str = Field(default="")
    undone_at: str = Field(default="", description="Set when undone, else empty.")


class Row(_Strict):
    """One mapped row from an outside read: values plus their source."""

    values: dict[str, Any] = Field(default_factory=dict)
    source: str = Field(default="", description="Which read produced this row.")


class ReadResult(_Strict):
    """What a connector read returned: mapped rows, counts, errors."""

    rows: list[Row] = Field(default_factory=list)
    rows_read: int = Field(default=0, description="Rows the outside tool returned.")
    rows_mapped: int = Field(default=0, description="Rows mapped into the book.")
    errors: list[str] = Field(default_factory=list)
