"""Shop replies waiting for their receipt job, deduped on the message id.

In memory for tests and the quick sim; ``SqlShopInbox`` persists the same
thing (Postgres via alembic ``0002_shop_inbox``, SQLite in tests) so
the console can read it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class ShopReply:
    message_id: str
    job_id: str
    shop_id: str
    sender: str
    text: str
    received_at: str  # ISO, WAT


class ShopInbox(Protocol):
    def add(self, reply: ShopReply) -> bool:
        """False when the message id was already seen (a webhook retry)."""
        ...

    def replies(self, job_id: str) -> list[ShopReply]: ...


class InMemoryShopInbox:
    def __init__(self) -> None:
        self._by_job: dict[str, list[ShopReply]] = {}
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def add(self, reply: ShopReply) -> bool:
        with self._lock:
            if reply.message_id in self._seen:
                return False
            self._seen.add(reply.message_id)
            self._by_job.setdefault(reply.job_id, []).append(reply)
            return True

    def replies(self, job_id: str) -> list[ShopReply]:
        with self._lock:
            return list(self._by_job.get(job_id, []))


metadata = sa.MetaData()
shop_inbox = sa.Table(
    "shop_inbox",
    metadata,
    sa.Column("message_id", sa.String(), primary_key=True),
    sa.Column("job_id", sa.String(), nullable=False, index=True),
    sa.Column("shop_id", sa.String(), nullable=False, index=True),
    sa.Column("sender", sa.String(), nullable=False),
    sa.Column("text", sa.Text(), nullable=False),
    sa.Column("received_at", sa.String(), nullable=False),
)


class SqlShopInbox:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def ensure_schema(self) -> None:
        metadata.create_all(self.engine)

    def add(self, reply: ShopReply) -> bool:
        with self.engine.begin() as conn:
            seen = conn.execute(
                sa.select(shop_inbox.c.message_id).where(
                    shop_inbox.c.message_id == reply.message_id
                )
            ).first()
            if seen is not None:
                return False
            conn.execute(shop_inbox.insert().values(**reply.__dict__))
            return True

    def replies(self, job_id: str) -> list[ShopReply]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                sa.select(shop_inbox)
                .where(shop_inbox.c.job_id == job_id)
                .order_by(shop_inbox.c.received_at, shop_inbox.c.message_id)
            ).mappings()
            return [ShopReply(**dict(r)) for r in rows]
