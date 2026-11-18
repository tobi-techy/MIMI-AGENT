"""CSV and Excel import, in-house (RAI-277): standard library ``csv``.

Excel files need ``openpyxl`` (``pip install -e '.[connectors]'``). Files
are parsed server side; bad files (empty, wrong format, over 10,000 rows)
fail with a plain message. The mapping rules are the sheet ones
(``sheets/mapping.py``): model suggests, shop confirms, zero numbers
invented.

The grid readers here are public because the book import
(``mimi.connectors.book``) reads the same two shapes through them: a
book's tables arrive as a folder of CSV files or as one workbook with one
sheet per table. One parser, one place a file quirk (a BOM, a quoted comma,
an Excel date cell) is understood.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from mimi.connectors.base import Connector, ConnectorError
from mimi.connectors.sheets.mapping import MAX_ROWS, map_rows, suggest_mapping
from mimi.connectors.types import Proposal, ReadResult, Row, WriteRecord

EXCEL_EXTENSIONS = (".xlsx", ".xlsm", ".xltx", ".xltm")


class CsvConnector(Connector):
    """A CSV or Excel upload, over the web or a WhatsApp document."""

    name = "csv"

    def __init__(self, *, tenant_id: str = "") -> None:
        self.tenant_id = tenant_id

    def read(self, resource: str, filters: dict[str, Any] | None = None) -> ReadResult:
        args = filters or {}
        text = args.get("text", "")
        filename = str(args.get("filename") or "")
        mapping = dict(args.get("mapping") or {})
        if filename.lower().endswith(EXCEL_EXTENSIONS):
            grid = excel_grid(args.get("bytes"), filename)
        elif filename.lower().endswith(".csv"):
            grid = csv_grid(text)
        elif text and not filename:
            grid = csv_grid(text)
        else:
            raise ConnectorError(
                "That file is not a CSV or Excel file.",
                next_step="Send a .csv or .xlsx file.")
        if len(grid) - 1 > MAX_ROWS:
            raise ConnectorError("That file is too big (over 10,000 rows).",
                                 next_step="Send a smaller file.")
        try:
            mapped = map_rows(grid, mapping)
        except ValueError as exc:
            raise ConnectorError(str(exc),
                                 next_step="Fix the mapping and confirm.") from exc
        return ReadResult(rows=[Row(values=m, source="csv") for m in mapped],
                          rows_read=max(0, len(grid) - 1),
                          rows_mapped=len(mapped), errors=[])

    def suggest_columns(self, header: list[str]) -> dict[str, str]:
        return suggest_mapping(header)

    def propose_write(self, action: str, payload: dict[str, Any]) -> Proposal:
        raise ConnectorError("CSV import never writes.",
                             next_step="Read the file; write nothing back.")

    def apply_write(self, proposal: Proposal) -> WriteRecord:
        raise ConnectorError("CSV import never writes.",
                             next_step="Read the file; write nothing back.")

    def undo(self, write: WriteRecord) -> WriteRecord:
        raise ConnectorError("There is no CSV write to undo.",
                             next_step="Nothing was ever written.")


def csv_grid(text: str) -> list[list[str]]:
    """One CSV file as a values grid, first row header. Raises ``ConnectorError``
    when the text is not a CSV or holds nothing."""
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise ConnectorError("That file is not a valid CSV.",
                             next_step="Send a valid .csv file.") from exc
    if not rows or not any(any(c.strip() for c in r) for r in rows):
        raise ConnectorError("That file is empty.",
                             next_step="Send a sheet with an item column.")
    return rows


def excel_sheets(data: Any, filename: str) -> dict[str, list[list[Any]]]:
    """Every sheet in a workbook, by the name it carries in the file.

    A book import reads one sheet per table, so it needs the names; the
    single-sheet upload (``CsvConnector``) uses ``excel_grid`` below. Values
    are the cached ones (``data_only``): a formula cell gives the number
    Excel last calculated, never the formula text.
    """
    try:
        import openpyxl
    except ImportError as exc:
        raise ConnectorError("Excel needs the connectors extra.",
                             next_step="Install it with pip install -e '.[connectors]'.") from exc
    raw = bytes(data or b"")
    if not raw:
        raise ConnectorError("That file is empty.",
                             next_step="Send a sheet with an item column.")
    try:
        book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        out = {sheet.title: [[c.value if c.value is not None else "" for c in row]
                             for row in sheet.iter_rows()]
               for sheet in book.worksheets}
    except Exception as exc:
        raise ConnectorError(f"I could not read {filename}.",
                             next_step="Send a valid .xlsx file.") from exc
    if not out or not any(any(str(c).strip() for c in r) for g in out.values() for r in g):
        raise ConnectorError("That file is empty.",
                             next_step="Send a sheet with an item column.")
    return out


def excel_grid(data: Any, filename: str) -> list[list[Any]]:
    """A workbook's first sheet as a values grid (an upload is one table)."""
    sheets = excel_sheets(data, filename)
    return next(iter(sheets.values()))
