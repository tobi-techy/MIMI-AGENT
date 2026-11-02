"""Mimi's connector interface (RAI-277): read a shop tool, propose writes, apply, undo.

Composio is the transport behind this interface: it does OAuth, token
storage and refresh, and the consent screen. Approval, idempotency, audit
and undo stay in Mimi code. The model never calls Composio directly; only
allowlisted actions are wrapped (``allowlist.py``).

Every connector speaks this protocol:

* ``read(resource, filters)``: rows from the outside tool, mapped into
  plain dicts. Reads need the shop's consent (``consent.py``).
* ``propose_write(action, payload)``: a ``Proposal`` with before and after
  values, an amount in kobo when money is named, and an idempotency key.
* ``apply_write(proposal)``: runs only for an owner-approved proposal.
* ``undo(write)``: reverses an applied write with the saved before values.

Names here are Mimi's own (``zoho_books_invoice_comment``). The Composio
slug each maps to lives in the allowlist, so swapping transport never
touches a caller.
"""

from __future__ import annotations

from typing import Any, Protocol

from mimi.connectors.types import Proposal, ReadResult, WriteRecord


class Connector(Protocol):
    """One outside tool behind Mimi's consent, approval and audit."""

    name: str

    def read(self, resource: str, filters: dict[str, Any] | None = None) -> ReadResult:
        """Rows from the outside tool. Raises ``ConnectorError`` on failure."""
        ...

    def propose_write(self, action: str, payload: dict[str, Any]) -> Proposal:
        """A proposal the owner can approve. Never applies anything."""
        ...

    def apply_write(self, proposal: Proposal) -> WriteRecord:
        """Apply an owner-approved proposal. Raises without approval."""
        ...

    def undo(self, write: WriteRecord) -> WriteRecord:
        """Reverse an applied write. Raises when the action has no undo."""
        ...


class ConnectorError(RuntimeError):
    """The outside tool, the transport or the consent check said no."""

    def __init__(self, message: str, *, next_step: str = "") -> None:
        super().__init__(message)
        self.next_step = next_step
