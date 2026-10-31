"""Mono stub (RAI-277): same interface, a fake only.

Mono (Nigerian open banking) is not on Composio, so the direct connector
lands later. This stub holds the interface shape and a fake transport so
callers can be written and tested now. Moniepoint and OPay are not on
Composio either; they stay on forwarded alerts and exported reports.
"""

from __future__ import annotations

from typing import Any

from mimi.connectors.base import Connector, ConnectorError
from mimi.connectors.types import Proposal, ReadResult, Row, WriteRecord


class MonoStub(Connector):
    """The Mono interface before the direct connector is built."""

    name = "mono"

    def __init__(self, *, tenant_id: str = "") -> None:
        self.tenant_id = tenant_id

    def read(self, resource: str, filters: dict[str, Any] | None = None) -> ReadResult:
        raise ConnectorError("Mono is not connected yet.",
                             next_step="Use forwarded alerts or exported reports.")

    def propose_write(self, action: str, payload: dict[str, Any]) -> Proposal:
        raise ConnectorError("Mono is not connected yet.",
                             next_step="Use forwarded alerts or exported reports.")

    def apply_write(self, proposal: Proposal) -> WriteRecord:
        raise ConnectorError("Mono is not connected yet.",
                             next_step="Use forwarded alerts or exported reports.")

    def undo(self, write: WriteRecord) -> WriteRecord:
        raise ConnectorError("Mono is not connected yet.",
                             next_step="Use forwarded alerts or exported reports.")


class FakeMono(MonoStub):
    """Recorded Mono replies for tests: daily sales value only (no item
    detail, so it never turns the weekly check-in off and is never used
    for credit)."""

    def __init__(self, values: list[dict[str, Any]] | None = None,
                 *, tenant_id: str = "") -> None:
        super().__init__(tenant_id=tenant_id)
        self._values = list(values or [])

    def read(self, resource: str, filters: dict[str, Any] | None = None) -> ReadResult:
        rows = [Row(values=dict(v), source="mono") for v in self._values]
        return ReadResult(rows=rows, rows_read=len(rows),
                          rows_mapped=len(rows), errors=[])
