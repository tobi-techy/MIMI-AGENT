"""The context builder's stores (RAI-281) kept in the database.

``ContextBuilder`` pins each thread's prefix, reads its history and stores
long tool outputs. In process those are dicts; here they are tables, so a
restarted API or a new worker builds the same bytes the last one did and the
provider's prompt cache keeps hitting.

History is written by the turn's completing transaction (keyed by turn id,
so a re-run turn never appends twice); ``append`` is kept for callers that
use the builder directly.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.harness.context import ArtifactStore, HistoryStore, PrefixStore, ThreadPrefix, sha
from mimi.store import schema as s
from mimi.store.util import dumps, insert_ignore, loads


class SqlPrefixStore(PrefixStore):
    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self.engine = engine

    def get(self, tenant: str, thread: str, helper: str) -> ThreadPrefix | None:
        with self.engine.connect() as conn:
            body = conn.execute(sa.select(s.prefixes.c.body).where(
                s.prefixes.c.tenant_id == tenant, s.prefixes.c.thread_id == thread,
                s.prefixes.c.helper == helper)).scalar()
        return ThreadPrefix.model_validate(loads(body)) if body else None

    def put(self, p: ThreadPrefix) -> None:
        body = p.model_dump_json()
        with self.engine.begin() as conn:
            if not insert_ignore(conn, s.prefixes, {
                    "tenant_id": p.tenant_id, "thread_id": p.thread_id, "helper": p.helper,
                    "body": body, "window_from": 0}):
                conn.execute(s.prefixes.update().where(
                    s.prefixes.c.tenant_id == p.tenant_id,
                    s.prefixes.c.thread_id == p.thread_id,
                    s.prefixes.c.helper == p.helper).values(body=body))

    def compact(self, tenant: str, thread: str, helper: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(s.prefixes.delete().where(
                s.prefixes.c.tenant_id == tenant, s.prefixes.c.thread_id == thread,
                s.prefixes.c.helper == helper))

    def window(self, tenant: str, thread: str, helper: str) -> int:
        with self.engine.connect() as conn:
            w = conn.execute(sa.select(s.prefixes.c.window_from).where(
                s.prefixes.c.tenant_id == tenant, s.prefixes.c.thread_id == thread,
                s.prefixes.c.helper == helper)).scalar()
        return int(w or 0)

    def move_window(self, tenant: str, thread: str, helper: str, start: int) -> None:
        with self.engine.begin() as conn:
            if not insert_ignore(conn, s.prefixes, {
                    "tenant_id": tenant, "thread_id": thread, "helper": helper, "body": None,
                    "window_from": start}):
                conn.execute(s.prefixes.update().where(
                    s.prefixes.c.tenant_id == tenant, s.prefixes.c.thread_id == thread,
                    s.prefixes.c.helper == helper).values(window_from=start))


class SqlHistoryStore(HistoryStore):
    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self.engine = engine

    def turns(self, tenant: str, thread: str) -> list[list[dict[str, Any]]]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(s.history.c.body).where(
                s.history.c.tenant_id == tenant, s.history.c.thread_id == thread)
                .order_by(s.history.c.ts, s.history.c.turn_id)).scalars().all()
        return [list(loads(b, [])) for b in rows]

    def append(self, tenant: str, thread: str, messages: Sequence[dict[str, Any]]) -> None:
        body = [dict(m) for m in messages]
        with self.engine.begin() as conn:
            append_history(conn, tenant, thread, "h_" + sha(body)[:24], body)

    def summary(self, tenant: str, thread: str) -> str:
        with self.engine.connect() as conn:
            return str(conn.execute(sa.select(s.threads.c.summary).where(
                s.threads.c.tenant_id == tenant, s.threads.c.thread_id == thread)).scalar()
                or "")

    def set_summary(self, tenant: str, thread: str, text: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(s.threads.update().where(
                s.threads.c.tenant_id == tenant, s.threads.c.thread_id == thread)
                .values(summary=text))


def append_history(conn: sa.Connection, tenant: str, thread: str, turn_id: str,
                   messages: Sequence[dict[str, Any]]) -> bool:
    return insert_ignore(conn, s.history, {
        "tenant_id": tenant, "thread_id": thread, "turn_id": turn_id, "ts": time.time(),
        "body": dumps([dict(m) for m in messages])})


class SqlArtifactStore(ArtifactStore):
    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self.engine = engine

    def put(self, tenant: str, content: str) -> str:
        path = f"artifact://{tenant}/{sha(content)}"
        with self.engine.begin() as conn:
            insert_ignore(conn, s.artifacts, {"path": path, "tenant_id": tenant,
                                              "content": content})
        return path

    def get(self, path: str) -> str:
        with self.engine.connect() as conn:
            content = conn.execute(sa.select(s.artifacts.c.content).where(
                s.artifacts.c.path == path)).scalar()
        if content is None:
            raise KeyError(path)
        return str(content)

    def __len__(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(sa.select(sa.func.count()).select_from(s.artifacts))
                       .scalar() or 0)
