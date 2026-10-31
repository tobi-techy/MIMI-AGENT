"""Nightly sync: pull connector rows into the shop book (RAI-277).

One sync run reads a connection's rows (consent checked first), maps
them into the book, and logs the run (rows read, rows mapped, errors).
POS item sales feed the estimator as exact per-item sales; terminal or
transfer daily value feeds it as a value signal split by delivery mix,
low confidence. The weekly check-in turns off for sheet and POS item
sources with fresh rows; a source with no new rows for 10 days turns it
back on. Terminal value data alone never turns it off.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.connectors import consent as ct
from mimi.connectors.base import Connector, ConnectorError
from mimi.store import schema as s
from mimi.store.util import dumps, now_iso
from mimi.tz import now_lagos

STALE_DAYS = 10


def sync(*, engine: Engine, tenant_id: str, connection_id: str,
         connector: Connector, shop_id: str = "", scope: str = "stock",
         today: str | None = None) -> dict[str, Any]:
    """Run one sync for one connection. Returns the run row."""
    day = today or now_lagos().date().isoformat()
    errors: list[str] = []
    rows_read, rows_mapped = 0, 0
    try:
        ct.check_read(engine, tenant_id, shop_id, scope)
        result = connector.read(_resource_for(connector, scope), {"shop_id": shop_id})
        rows_read, rows_mapped, run_errors = _apply(engine, tenant_id, result.rows,
                                                    shop_id=shop_id, day=day)
        errors.extend(result.errors)
        errors.extend(run_errors)
    except ConnectorError as exc:
        errors.append(str(exc))
    run = {"tenant_id": tenant_id, "connection_id": connection_id, "shop_id": shop_id,
           "scope": scope, "rows_read": rows_read, "rows_mapped": rows_mapped,
           "errors": dumps(errors), "started_at": now_iso(), "finished_at": now_iso(),
           "run_id": _run_id(tenant_id, connection_id, day)}
    with engine.begin() as conn:
        conn.execute(sa.insert(s.connector_sync_runs).values(**run))
    return run


def source_fresh(engine: Engine, tenant_id: str, shop_id: str,
                 today: str | None = None) -> bool:
    """True when an item source synced rows in the last 10 days."""
    day = today or now_lagos().date().isoformat()
    cutoff = (date.fromisoformat(day) - timedelta(days=STALE_DAYS)).isoformat()
    with engine.connect() as conn:
        hit = conn.execute(sa.select(sa.literal(1)).where(sa.exists(sa.select(1).select_from(
            s.connector_sync_runs).where(
                s.connector_sync_runs.c.tenant_id == tenant_id,
                s.connector_sync_runs.c.shop_id == shop_id,
                s.connector_sync_runs.c.scope.in_(["stock", "sales"]),
                s.connector_sync_runs.c.rows_mapped > 0,
                s.connector_sync_runs.c.finished_at >= cutoff))))).scalar()
        return bool(hit)


def _resource_for(connector: Connector, scope: str) -> str:
    if connector.name == "zoho_books":
        return {"stock": "zoho_books_items", "sales": "zoho_books_invoices",
                "receivables": "zoho_books_invoices"}.get(scope, "zoho_books_items")
    if connector.name == "zoho_crm":
        return "zoho_crm_accounts"
    if connector.name == "sheets":
        return "sheets_values"
    if connector.name == "csv":
        return "csv"
    return "mono"


def _apply(engine: Engine, tenant_id: str, rows: Any, *,
           shop_id: str, day: str) -> tuple[int, int, list[str]]:
    """Map rows into ``stock_counts`` with source=connector. Returns
    (rows_read, rows_mapped, errors)."""
    errors: list[str] = []
    mapped = 0
    with engine.begin() as conn:
        for row in rows:
            values = row.values if hasattr(row, "values") else dict(row)
            item = str(values.get("item") or values.get("sku") or values.get("name") or "")
            qty = values.get("qty_left", values.get("quantity"))
            if not item or not isinstance(qty, int):
                continue
            conn.execute(sa.insert(s.stock_counts).values(
                tenant_id=tenant_id, shop_id=shop_id or str(values.get("shop_id") or ""),
                sku=str(values.get("sku") or item), item=item, quantity=qty,
                counted_on=day, source="connector"))
            mapped += 1
    return len(rows), mapped, errors


def _run_id(tenant_id: str, connection_id: str, day: str) -> str:
    return hashlib.sha256(f"{tenant_id}:{connection_id}:{day}".encode()).hexdigest()[:24]
