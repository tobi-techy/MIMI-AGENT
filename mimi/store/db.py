"""The store's database: one SQLAlchemy engine, SQLite by default.

``DATABASE_URL`` (or the older ``MIMI_DB_URL``, which alembic also reads)
picks the database. Unset, it is ``sqlite:///./mimi.db``. Postgres needs the
``postgres`` extra (``psycopg2-binary``). The URL is never stored in the repo.

SQLite is set up for several processes at once (the API and its workers):
WAL journal, a 30 s busy timeout and ``BEGIN IMMEDIATE`` on every
transaction, so two writers queue instead of failing on a lock upgrade.

**Connections are a budget, not a default.** Every Postgres engine is a
bounded ``QueuePool``: ``MIMI_DB_POOL_SIZE`` (5) plus
``MIMI_DB_MAX_OVERFLOW`` (5), waiting at most ``MIMI_DB_POOL_TIMEOUT_S`` (5 s)
before it raises ``TimeoutError`` instead of queueing for ever. The full
arithmetic, the database limit and what happens under saturation are in
``docs/DATA.md``; ``connection_budget()`` computes the same sum so a deploy
can check it.

**A slow query is visible before a customer complains.** Every engine times
every statement and logs a WARNING over ``MIMI_SLOW_QUERY_MS`` (200 ms), with
the statement text (parameters are never logged) and the elapsed time, and
keeps the last hundred in ``engine.info["mimi_slow_queries"]``. The budget
each hot path is held to is in ``mimi.store.budgets``.
"""

from __future__ import annotations

import collections
import logging
import os
import re
import time
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

DEFAULT_URL = "sqlite:///./mimi.db"

#: Pool defaults, per process. Five used connections and five burst ones is
#: the pilot budget; ``connection_budget`` adds the processes up.
POOL_SIZE = 5
MAX_OVERFLOW = 5
POOL_TIMEOUT_S = 5.0
POOL_RECYCLE_S = 1800

#: The database's own limit (Postgres ``max_connections``). A deploy must stay
#: under this with room for alembic, monitoring and an admin session.
DEFAULT_MAX_CONNECTIONS = 100
RESERVED_CONNECTIONS = 10

#: Any statement over this is logged and counted. ``mimi.store.budgets``
#: holds the tighter per-path budgets the tests enforce.
DEFAULT_SLOW_QUERY_MS = 200.0

log = logging.getLogger("mimi.store")
slow_log = logging.getLogger("mimi.store.slow")

#: Per-engine counters. ``Engine`` carries no ``info`` mapping of its own, and
#: a module dict keyed weakly by the engine does not make the engine outlive
#: the process.
_SLOW_STATE: weakref.WeakKeyDictionary[Engine, dict[str, Any]] = (
    weakref.WeakKeyDictionary())

_DIGITS = re.compile(r"\d{9,}")


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


@dataclass(frozen=True)
class PoolSettings:
    """One process's connection pool, and the budget it implies."""

    size: int = POOL_SIZE
    max_overflow: int = MAX_OVERFLOW
    timeout_s: float = POOL_TIMEOUT_S
    recycle_s: int = POOL_RECYCLE_S

    @property
    def per_process(self) -> int:
        return self.size + self.max_overflow


def _env_int(e: Mapping[str, str], name: str, default: int, *, minimum: int) -> int:
    raw = (e.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number, not {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, not {value}")
    return value


def _env_float(e: Mapping[str, str], name: str, default: float, *, above: float) -> float:
    raw = (e.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, not {raw!r}") from exc
    if value <= above:
        raise ValueError(f"{name} must be greater than {above}, not {value}")
    return value


def pool_settings(env: Mapping[str, str] | None = None) -> PoolSettings:
    """The pool for one process, from the environment, validated at startup.

    A bad number fails the process at start, not at the first busy minute:
    ``POOL_SIZE=0`` or a negative overflow is a typo, never a policy.
    """
    e = env if env is not None else os.environ
    return PoolSettings(
        size=_env_int(e, "MIMI_DB_POOL_SIZE", POOL_SIZE, minimum=1),
        max_overflow=_env_int(e, "MIMI_DB_MAX_OVERFLOW", MAX_OVERFLOW, minimum=0),
        timeout_s=_env_float(e, "MIMI_DB_POOL_TIMEOUT_S", POOL_TIMEOUT_S, above=0.0),
        recycle_s=_env_int(e, "MIMI_DB_POOL_RECYCLE_S", POOL_RECYCLE_S, minimum=0),
    )


def connection_budget(instances: int = 1, *, settings: PoolSettings | None = None,
                      limit: int | None = None,
                      reserved: int = RESERVED_CONNECTIONS) -> dict[str, Any]:
    """The connection arithmetic for a deploy.

    ``instances`` is how many API/worker processes share one database. The
    returned ``fits`` is False when the pools plus the reserved connections
    would exceed ``limit`` (Postgres ``max_connections``, 100 by default), so
    a scaling change can be checked before it is rolled out.
    """
    if instances < 1:
        raise ValueError("instances must be at least 1")
    s = settings or pool_settings()
    cap = limit if limit is not None else int(
        os.environ.get("MIMI_DB_MAX_CONNECTIONS", "") or DEFAULT_MAX_CONNECTIONS)
    total = instances * s.per_process
    return {"instances": instances, "pool_size": s.size, "max_overflow": s.max_overflow,
            "per_instance": s.per_process, "pool_total": total, "reserved": reserved,
            "limit": cap, "headroom": cap - total - reserved,
            "fits": total + reserved <= cap}


def _redact(statement: str) -> str:
    """The statement as one short line: no parameters, no long digit runs."""
    text = " ".join(str(statement).split())
    text = _DIGITS.sub("<digits>", text)
    return text[:400]


def install_slow_query_log(engine: Engine, threshold_ms: float | None = None) -> None:
    """Time every statement; warn over ``threshold_ms`` and keep the last 100.

    Idempotent: installing twice would double-count, so the second call is a
    no-op. The listener never raises: a logging bug must not fail a query.
    """
    if engine in _SLOW_STATE:
        return
    threshold = (threshold_ms if threshold_ms is not None else
                 float(os.environ.get("MIMI_SLOW_QUERY_MS", "") or DEFAULT_SLOW_QUERY_MS))
    if threshold <= 0:
        threshold = DEFAULT_SLOW_QUERY_MS
    state: dict[str, Any] = {"ms": threshold, "count": 0,
                             "recent": collections.deque(maxlen=100)}
    _SLOW_STATE[engine] = state

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _before(conn: Any, cursor: Any, statement: str, parameters: Any,
                context: Any, executemany: bool) -> None:
        context._mimi_query_started = time.perf_counter()

    @sa.event.listens_for(engine, "after_cursor_execute")
    def _after(conn: Any, cursor: Any, statement: str, parameters: Any,
               context: Any, executemany: bool) -> None:
        started = getattr(context, "_mimi_query_started", None)
        if started is None:
            return
        elapsed = (time.perf_counter() - started) * 1000.0
        if elapsed < threshold:
            return
        try:
            text = _redact(statement)
            state["recent"].append({"ms": round(elapsed, 1), "statement": text})
            state["count"] += 1
            slow_log.warning("slow query %.1f ms (budget %.0f ms): %s", elapsed, threshold,
                             text, extra={"mimi_slow_query": True,
                                          "elapsed_ms": round(elapsed, 1)})
        except Exception:  # pragma: no cover - never fail the query for a log line
            pass


def slow_query_state(engine: Engine) -> dict[str, Any] | None:
    """The slow-query counters for an engine (None if the log is not installed)."""
    return _SLOW_STATE.get(engine)


def make_engine(url: str | None = None, *, env: Mapping[str, str] | None = None) -> Engine:
    url = normalize_url(url or database_url(env))
    e = env if env is not None else os.environ
    settings = pool_settings(e)
    if url.startswith("sqlite"):
        memory = url in ("sqlite://", "sqlite:///:memory:") or "mode=memory" in url
        kw: dict[str, Any] = (
            {"poolclass": sa.pool.StaticPool} if memory else
            {"pool_size": settings.size,
             # SQLite writers queue on the file lock; keep the old long wait so
             # a busy writer is a pause, not an error.
             "max_overflow": settings.max_overflow * 2,
             "pool_timeout": 30.0})
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

    else:
        engine = sa.create_engine(
            url,
            pool_size=settings.size,
            max_overflow=settings.max_overflow,
            pool_timeout=settings.timeout_s,
            pool_recycle=(settings.recycle_s or None),
            # A dropped connection is detected before the query, not during it.
            pool_pre_ping=True,
            # Reuse the busiest connection first, so idle ones are reaped.
            pool_use_lifo=True,
            connect_args={"application_name": (e.get("MIMI_DB_APP_NAME", "") or "mimi")},
        )
    install_slow_query_log(engine, float(e.get("MIMI_SLOW_QUERY_MS", "") or 0) or None)
    return engine


def ensure_schema(engine: Engine) -> None:
    """Create the store's tables (tests and a quick local try; alembic in production)."""
    from mimi.store.schema import metadata

    metadata.create_all(engine)
