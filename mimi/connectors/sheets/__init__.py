"""Google Sheets connector (RAI-277): reads only."""

from mimi.connectors.sheets.connector import SheetsConnector
from mimi.connectors.sheets.mapping import map_rows, match_sku, suggest_mapping

__all__ = ["SheetsConnector", "map_rows", "match_sku", "suggest_mapping"]
