"""Connector reads for the console (RAI-277): toolkits, accounts, sync preview.

Every query is filtered by the caller's tenant. No tokens are ever
returned: Composio holds them.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.api.agent import NotFound
from mimi.connectors.allowlist import ACTIONS
from mimi.store import schema as s
from mimi.store.util import dumps, loads

TOOLKITS: tuple[dict[str, str], ...] = (
    {"toolkit": "ZOHO_BOOKS", "label": "Zoho Books",
     "blurb": "Customers, invoices, payments and items from the books."},
    {"toolkit": "ZOHO", "label": "Zoho CRM",
     "blurb": "Accounts and contacts from the CRM."},
    {"toolkit": "GOOGLESHEETS", "label": "Google Sheets",
     "blurb": "A shop's stock sheet, read-only."},
    {"toolkit": "CSV", "label": "CSV or Excel upload",
     "blurb": "A file upload, parsed in-house."},
    {"toolkit": "MONO", "label": "Mono (soon)",
     "blurb": "Bank data over open banking. Not connected yet."},
)


def list_connectors(engine: Engine, tenant: str) -> dict[str, Any]:
    """Every toolkit with its connection status for this distributor, and
    whether this server can start a real connect flow for it (``connect``)."""
    from mimi.connectors.env import ConnectorSettings

    settings = ConnectorSettings.from_env()
    with engine.connect() as conn:
        rows = list(conn.execute(sa.select(s.connector_accounts).where(
            s.connector_accounts.c.tenant_id == tenant)).all())
    by_toolkit: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_toolkit.setdefault(r.toolkit, []).append({
            "connection_id": r.connection_id, "status": r.status,
            "region": r.region, "connected_at": r.connected_at,
            "revoked_at": r.revoked_at})
    return {"connectors": [
        {"toolkit": t["toolkit"], "label": t["label"], "blurb": t["blurb"],
         "reads": sorted({a.action for a in ACTIONS if a.toolkit == t["toolkit"]
                           and a.kind == "read"}),
         "writes": sorted({a.action for a in ACTIONS if a.toolkit == t["toolkit"]
                            and a.kind == "write"}),
         "connections": by_toolkit.get(t["toolkit"], []),
         "connect": _connect(settings, t["toolkit"])}
        for t in TOOLKITS]}


def _connect(settings: Any, toolkit: str) -> dict[str, Any]:
    ok, reason = settings.connect_status(toolkit)
    return {"available": ok, "reason": reason}


def read_connection(engine: Engine, tenant: str, connection_id: str,
                    transport: Any) -> dict[str, Any]:
    """A live read from one active connection, so the owner sees Mimi can
    really read the account: the organization and the first customers for
    Zoho Books, the first contacts for Zoho CRM. Read-only."""
    from mimi.store.util import now_iso

    with engine.connect() as conn:
        a = conn.execute(sa.select(s.connector_accounts).where(
            s.connector_accounts.c.tenant_id == tenant,
            s.connector_accounts.c.connection_id == connection_id)).first()
    if a is None or a.status != "active":
        raise NotFound(connection_id)
    account = str(a.connected_account_id or connection_id)
    toolkit = str(a.toolkit or "").upper()
    items: list[dict[str, str]] = []
    if toolkit == "ZOHO_BOOKS":
        from mimi.connectors.zoho.books import ZohoBooksConnector

        books = ZohoBooksConnector(transport, tenant_id=tenant, connection_id=account)
        orgs = books.read("zoho_books_organizations").rows
        contacts = books.read("zoho_books_contacts").rows
        org = str(orgs[0].values.get("name")) if orgs else "your Zoho Books"
        for r in contacts[:5]:
            owed = int(r.values.get("outstanding_kobo") or 0) // 100
            items.append({"label": str(r.values.get("name") or "Customer"),
                          "detail": f"owes ₦{owed:,}" if owed else ""})
        summary = (f"Read {len(contacts)} customer{'s' if len(contacts) != 1 else ''} "
                   f"from {org}.")
    elif toolkit == "ZOHO":
        from mimi.connectors.zoho.crm import ZohoCrmConnector

        crm = ZohoCrmConnector(transport, tenant_id=tenant, connection_id=account)
        rows = crm.read("zoho_crm_accounts").rows
        for r in rows[:5]:
            items.append({"label": str(r.values.get("name") or "Account"), "detail": ""})
        summary = f"Read {len(rows)} account{'s' if len(rows) != 1 else ''} from Zoho CRM."
    else:
        summary = "Connected. This tool is read when Mimi syncs a shop's sheet."
    return {"connection_id": connection_id, "toolkit": toolkit,
            "read_at": now_iso(), "summary": summary, "items": items}


def get_connection(engine: Engine, tenant: str, connection_id: str) -> dict[str, Any]:
    """One connection with a sync preview: counts and the first 3 mapped rows."""
    with engine.connect() as conn:
        a = conn.execute(sa.select(s.connector_accounts).where(
            s.connector_accounts.c.tenant_id == tenant,
            s.connector_accounts.c.connection_id == connection_id)).first()
        if a is None:
            raise NotFound(connection_id)
        runs = list(conn.execute(sa.select(s.connector_sync_runs).where(
            s.connector_sync_runs.c.tenant_id == tenant,
            s.connector_sync_runs.c.connection_id == connection_id).order_by(
                s.connector_sync_runs.c.finished_at.desc()).limit(1)).all())
        last = runs[0] if runs else None
        preview_rows = list(conn.execute(sa.select(
            s.stock_counts).where(s.stock_counts.c.tenant_id == tenant).order_by(
                s.stock_counts.c.counted_on.desc()).limit(3)).all())
    out: dict[str, Any] = {
        "connection_id": a.connection_id, "toolkit": a.toolkit, "status": a.status,
        "region": a.region, "connected_at": a.connected_at, "revoked_at": a.revoked_at,
        "last_sync": None, "preview": []}
    if last is not None:
        out["last_sync"] = {"rows_read": last.rows_read, "rows_mapped": last.rows_mapped,
                            "errors": loads(last.errors, []),
                            "finished_at": last.finished_at}
    out["preview"] = [{"shop_id": r.shop_id, "sku": r.sku, "item": r.item,
                       "quantity": r.quantity, "counted_on": r.counted_on}
                      for r in preview_rows]
    return out


def save_account(engine: Engine, tenant: str, *, connection_id: str, toolkit: str,
                 connected_account_id: str, region: str, connected_at: str) -> None:
    with engine.begin() as conn:
        conn.execute(sa.insert(s.connector_accounts).values(
            tenant_id=tenant, connection_id=connection_id, toolkit=toolkit,
            connected_account_id=connected_account_id, region=region,
            status="active", connected_at=connected_at, revoked_at=None))


def revoke_account(engine: Engine, tenant: str, connection_id: str,
                   revoked_at: str) -> bool:
    """Write the revoke row before the next sync. True when revoked."""
    with engine.begin() as conn:
        res = conn.execute(s.connector_accounts.update().where(
            s.connector_accounts.c.tenant_id == tenant,
            s.connector_accounts.c.connection_id == connection_id,
            s.connector_accounts.c.status == "active").values(
                status="revoked", revoked_at=revoked_at))
        return bool(res.rowcount)


def save_write(engine: Engine, tenant: str, record: Any) -> None:
    with engine.begin() as conn:
        conn.execute(sa.insert(s.connector_writes).values(
            tenant_id=tenant, write_id=record.write_id, proposal_id=record.proposal_id,
            idempotency_key=record.idempotency_key, action=record.action,
            payload_hash=record.payload_hash, before=dumps(record.before),
            after=dumps(record.after), external_id=record.external_id,
            approval_id=record.approval_id, applied_at=record.applied_at,
            undone_at=record.undone_at or None))


def mark_undone(engine: Engine, tenant: str, write_id: str, undone_at: str) -> None:
    with engine.begin() as conn:
        conn.execute(s.connector_writes.update().where(
            s.connector_writes.c.tenant_id == tenant,
            s.connector_writes.c.write_id == write_id).values(undone_at=undone_at))
