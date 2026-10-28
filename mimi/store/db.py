"""The store's database: one SQLAlchemy engine, SQLite by default.

``DATABASE_URL`` (or the older ``MIMI_DB_URL``, which alembic also reads)
picks the database. Unset, it is ``sqlite:///./mimi.db``. Postgres needs the
``postgres`` extra (``psycopg2-binary``). The URL is never stored in the repo.

SQLite is set up for several processes at once (the API and its workers):
WAL journal, a 30 s busy timeout and ``BEGIN IMMEDIATE`` on every
transaction, so two writers queue instead of failing on a lock upgrade.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

DEFAULT_URL = "sqlite:///./mimi.db"


def normalize_url(url: str) -> str:
    """Pin Postgres to psycopg2 (the ``postgres`` extra).

    Hosted Postgres often hands out ``postgres://``, and SQLAlchemy 2.1 maps a
    bare ``postgresql://`` to psycopg 3, which this package does not install.
    """
    for bare in ("postgres://", "postgresql://"):
        if url.startswith(bare):
            return "postgresql+psycopg2://" + url[len(bare):]
    return url


def database_url(env: Mapping[str, str] | None = None) -> str:
    e = env if env is not None else os.environ
    for name in ("DATABASE_URL", "MIMI_DB_URL"):
        url = e.get(name, "").strip()
        if url:
            return normalize_url(url)
    return DEFAULT_URL


def make_engine(url: str | None = None) -> Engine:
    url = normalize_url(url or database_url())
    if url.startswith("sqlite"):
        memory = url in ("sqlite://", "sqlite:///:memory:") or "mode=memory" in url
        kw: dict[str, Any] = {"poolclass": sa.pool.StaticPool} if memory else {}
        engine = sa.create_engine(url, connect_args={"check_same_thread": False,
                                                     "timeout": 30}, **kw)

        @sa.event.listens_for(engine, "connect")
        def _connect(dbapi_conn: Any, _rec: Any) -> None:
            # Let SQLAlchemy's "begin" event issue BEGIN, not the driver.
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA busy_timeout = 30000")
            if not memory:
                cur.execute("PRAGMA journal_mode = WAL")
            cur.close()

        @sa.event.listens_for(engine, "begin")
        def _begin(conn: sa.Connection) -> None:
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        return engine
    return sa.create_engine(url, pool_pre_ping=True)


def ensure_schema(engine: Engine) -> None:
    """Create the store's tables (tests and a quick local try; alembic in production)."""
    from mimi.store.schema import metadata

    metadata.create_all(engine)
