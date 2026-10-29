"""Small helpers every store module shares: time stamps, JSON, insert-or-ignore."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from mimi.clock import wall


def now_iso(ts: float | None = None) -> str:
    """An ISO 8601 time with its zone (UTC)."""
    return datetime.fromtimestamp(wall() if ts is None else ts, UTC).isoformat()


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def loads(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return {} if default is None else default
    return json.loads(raw)


def insert_ignore(conn: sa.Connection, table: sa.Table, values: dict[str, Any]) -> bool:
    """Insert one row unless its key exists. True when this call wrote it."""
    name = conn.dialect.name
    if name == "sqlite":
        stmt: Any = sqlite.insert(table).values(**values).on_conflict_do_nothing()
    elif name == "postgresql":
        stmt = postgresql.insert(table).values(**values).on_conflict_do_nothing()
    else:  # pragma: no cover - other dialects: check first (fine inside one txn)
        keys = [c for c in table.primary_key.columns]
        cond = sa.and_(*[c == values[c.name] for c in keys])
        if conn.execute(sa.select(sa.literal(1)).select_from(table).where(cond)).first():
            return False
        stmt = table.insert().values(**values)
    return bool(conn.execute(stmt).rowcount == 1)


def mask_phone(phone: str) -> str:
    """The last four digits only: what the console and the logs may show."""
    digits = "".join(c for c in phone if c.isdigit())
    return f"***{digits[-4:]}" if digits else ""
