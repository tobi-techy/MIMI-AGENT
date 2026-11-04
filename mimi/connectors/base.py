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

import logging
import os
import re
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


class ConnectorRefusal(ConnectorError):
    """A refusal Mimi's own code wrote in plain words before anything was
    sent (no org named, no such customer, the invoice owes less). Safe to
    show the owner as it is; every other ``ConnectorError`` is not."""


# Anything that looks like a credential, scrubbed from server-side logs.
_SECRET_SHAPES = re.compile(
    r"(?i)(bearer\s+\S+|(?:api[_-]?key|token|secret|password|authorization)"
    r"[\"']?\s*[:=]\s*[\"']?[^\s,;\"'&]+|\b(?:ak|sk|pk)_[A-Za-z0-9_\-]{8,}"
    r"|\b[A-Za-z0-9_\-]{32,}\b)")
_SECRET_ENV = ("COMPOSIO_API_KEY", "MIMI_CONNECTOR_STATE_SECRET", "MONO_SECRET_KEY")


def redact(text: str) -> str:
    """The text with credential-shaped runs and our own secrets replaced."""
    out = str(text)
    for name in _SECRET_ENV:
        value = os.environ.get(name, "")
        if len(value) >= 6:
            out = out.replace(value, "[redacted]")
    return _SECRET_SHAPES.sub("[redacted]", out)[:500]


def log_failure(log: logging.Logger, where: str, exc: BaseException) -> None:
    """Log an outside tool's failure for us, never for the owner: the
    exception type and its redacted text, no secrets."""
    log.warning("%s failed: %s: %s", where, type(exc).__name__, redact(str(exc)))
