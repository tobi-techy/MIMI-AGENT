"""Digest queue and per-tenant speak counts (RAI-300) over the store.

``SqlDigestQueue`` is the owner's digest queue the daily note (RAI-289)
reads; ``SqlSpeakCounts`` is the per-audience daily counts the cap reads.
Both are the durable ``mimi.harness.speak`` ports behind ``SqlGo`` in the
API; tests and evals use ``InMemorySpeakStore``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.harness.speak import Audience
from mimi.store import schema as s
from mimi.store.util import now_iso


class SqlSpeakCounts:
    """Per-tenant unprompted counts and the owner's digest queue, in SQL."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def unprompted_today(self, tenant_id: str, audience: Audience, shop_id: str,
                         day: date) -> int:
        with self.engine.connect() as conn:
            n = conn.execute(sa.select(sa.func.count()).select_from(s.speak_counts).where(
                s.speak_counts.c.tenant_id == tenant_id,
                s.speak_counts.c.audience == audience,
                s.speak_counts.c.shop_id == (shop_id or ""),
                s.speak_counts.c.day == day.isoformat())).scalar()
        return int(n or 0)

    def record_unprompted(self, tenant_id: str, audience: Audience, shop_id: str,
                          day: date) -> None:
        with self.engine.begin() as conn:
            conn.execute(s.speak_counts.insert().values(
                tenant_id=tenant_id, audience=audience, shop_id=shop_id or "",
                day=day.isoformat(), created_at=now_iso()))

    def add(self, tenant_id: str, shop_id: str, body: str, kind: str,
            day: date, reason: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(s.digest_queue.insert().values(
                tenant_id=tenant_id, shop_id=shop_id or "", body=body, kind=kind,
                day=day.isoformat(), reason=reason, consumed=False,
                created_at=now_iso()))

    def pending(self, tenant_id: str, day: date) -> list[dict[str, Any]]:
        """The digest items the daily note has not consumed yet."""
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(s.digest_queue).where(
                s.digest_queue.c.tenant_id == tenant_id,
                s.digest_queue.c.day == day.isoformat(),
                s.digest_queue.c.consumed.is_(False)).order_by(
                    s.digest_queue.c.item_id)).all()
        return [dict(r._mapping) for r in rows]


SqlDigestQueue = SqlSpeakCounts


def ensure_speak_schema(engine: Engine) -> None:
    """Create the two tables when the migration has not run (tests)."""
    s.speak_counts.create(engine, checkfirst=True)
    s.digest_queue.create(engine, checkfirst=True)


__all__ = ["SqlDigestQueue", "SqlSpeakCounts", "ensure_speak_schema"]
