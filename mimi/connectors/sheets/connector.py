"""Google Sheets connector: reads only (RAI-277).

This issue never writes to shop sheets. The connector pulls a chosen
sheet on the nightly sync (not triggers), maps its columns to the book,
and stops asking the weekly question for sheet sources with fresh item
rows. A sheet with no new rows for 10 days re-enables the weekly
check-in. Value-only data never turns it off.
"""

from __future__ import annotations

from typing import Any

from mimi.connectors import allowlist
from mimi.connectors.base import Connector, ConnectorError
from mimi.connectors.composio import ComposioTransport, FakeComposio
from mimi.connectors.sheets.mapping import map_rows, suggest_mapping
from mimi.connectors.types import Proposal, ReadResult, Row, WriteRecord

_SHEET_READS = {
    "sheets_spreadsheet_info",
    "sheets_sheet_names",
    "sheets_values",
    "sheets_values_batch",
}


class SheetsConnector(Connector):
    """One shop's Google Sheet, read-only, behind the Composio transport."""

    name = "sheets"

    def __init__(self, transport: ComposioTransport | FakeComposio,
                 *, tenant_id: str, connection_id: str) -> None:
        self.transport = transport
        self.tenant_id = tenant_id
        self.connection_id = connection_id

    def read(self, resource: str, filters: dict[str, Any] | None = None) -> ReadResult:
        if resource not in _SHEET_READS or not allowlist.is_read(resource):
            raise ConnectorError(f"{resource} is not a wrapped Sheets read.",
                                 next_step="Use the info, names or values reads.")
        raw = self.transport.execute(resource, user_id=self.tenant_id,
                                     connected_account_id=self.connection_id,
                                     arguments=dict(filters or {}))
        grid = _grid(raw)
        mapping = dict((filters or {}).get("mapping") or {})
        rows = [Row(values=m, source=resource) for m in map_rows(grid, mapping)]
        return ReadResult(rows=rows, rows_read=max(0, len(grid) - 1),
                          rows_mapped=len(rows), errors=[])

    def suggest_columns(self, header: list[str]) -> dict[str, str]:
        """The model's first suggestion for the column mapping; the shop
        confirms it before it is ever saved."""
        return suggest_mapping(header)

    def propose_write(self, action: str, payload: dict[str, Any]) -> Proposal:
        raise ConnectorError("Mimi never writes to shop sheets.",
                             next_step="Read the sheet; write nothing back.")

    def apply_write(self, proposal: Proposal) -> WriteRecord:
        raise ConnectorError("Mimi never writes to shop sheets.",
                             next_step="Read the sheet; write nothing back.")

    def undo(self, write: WriteRecord) -> WriteRecord:
        raise ConnectorError("There is no Sheets write to undo.",
                             next_step="Nothing was ever written.")


def _grid(raw: dict[str, Any]) -> list[list[Any]]:
    values = raw.get("values") or raw.get("valueRanges") or []
    if isinstance(values, dict):
        values = values.get("values", [])
    if values and isinstance(values[0], dict):
        grid: list[list[Any]] = []
        for block in values:
            grid.extend(block.get("values", []))
        return grid
    if values and isinstance(values[0], list):
        return [list(r) for r in values]
    return []
