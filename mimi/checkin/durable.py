"""The weekly check-in's state in the SQL store (sales tracking, 3 Nov 2026).

``state.py`` keeps check-in records, skips and streaks in process memory: fine
for one worker, lost on a restart, and two workers would each send the same
week's question. These classes keep the same methods over two tables
(``checkin_weeks``, ``checkin_shops``, alembic 0015), so ``MIMI_CHECKIN`` can
be turned on safely. The flag stays off by default.

* ``SqlCheckinStore``: one row per (tenant, shop, ISO week). ``start`` is an
  insert that ignores a row already there, so the same event id, a retry or a
  second worker returns the existing record and ``is_new`` False: one message.
* ``SqlSkipLedger`` and ``SqlStreaks``: one row per shop with consecutive
  skips, the answered streak and whether the one Connect offer went out.

Every write is its own transaction; reads open their own connection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.checkin.state import CONNECT_AFTER_SKIPS, STREAK_CREDIT_LINE_AFTER, CheckinRecord
from mimi.clock import wall
from mimi.store import schema as s
from mimi.store.util import insert_ignore, now_iso

log = logging.getLogger(__name__)


def _rec(r: Any) -> CheckinRecord:
    return CheckinRecord(r.shop_id, r.week, r.event_id, r.status, r.low_stock_sku or "",
                         r.answered_at or "")


@dataclass
class SqlCheckinStore:
    engine: Engine
    tenant_id: str

    def _where(self, shop_id: str, week: str) -> Any:
        w = s.checkin_weeks.c
        return sa.and_(w.tenant_id == self.tenant_id, w.shop_id == shop_id, w.week == week)

    def get(self, shop_id: str, week: str) -> CheckinRecord | None:
        with self.engine.connect() as conn:
            r = conn.execute(sa.select(s.checkin_weeks).where(self._where(shop_id, week))).first()
        return _rec(r) if r is not None else None

    def seen(self, event_id: str) -> bool:
        w = s.checkin_weeks.c
        with self.engine.connect() as conn:
            return conn.execute(sa.select(w.week).where(
                w.tenant_id == self.tenant_id, w.event_id == event_id)).first() is not None

    def has(self, shop_id: str, week: str) -> bool:
        return self.get(shop_id, week) is not None

    def start(self, shop_id: str, week: str, event_id: str,
              low_stock_sku: str = "") -> tuple[CheckinRecord, bool]:
        with self.engine.begin() as conn:
            fresh = insert_ignore(conn, s.checkin_weeks, {
                "tenant_id": self.tenant_id, "shop_id": shop_id, "week": week,
                "event_id": event_id, "status": "asked", "low_stock_sku": low_stock_sku,
                "answered_at": "", "updated_at": now_iso(wall())})
            r = conn.execute(sa.select(s.checkin_weeks).where(self._where(shop_id, week))).one()
        return _rec(r), fresh

    def _move(self, shop_id: str, week: str, status: str, *, only_from: str | None,
              **extra: str) -> CheckinRecord | None:
        with self.engine.begin() as conn:
            q = s.checkin_weeks.update().where(self._where(shop_id, week))
            if only_from is not None:
                q = q.where(s.checkin_weeks.c.status == only_from)
            res = conn.execute(q.values(status=status, updated_at=now_iso(wall()), **extra))
            if res.rowcount != 1:
                return None
            r = conn.execute(sa.select(s.checkin_weeks).where(self._where(shop_id, week))).one()
        return _rec(r)

    def answer(self, shop_id: str, week: str, at: str = "") -> CheckinRecord | None:
        return self._move(shop_id, week, "answered", only_from="asked", answered_at=at)

    def skip(self, shop_id: str, week: str) -> CheckinRecord | None:
        return self._move(shop_id, week, "skipped", only_from="asked")

    def stop(self, shop_id: str, week: str) -> CheckinRecord | None:
        return self._move(shop_id, week, "stopped", only_from=None)

    def status(self, shop_id: str, week: str) -> str:
        rec = self.get(shop_id, week)
        return rec.status if rec else ""

    def answered_weeks(self, shop_id: str) -> list[str]:
        """The ISO weeks the shop answered, oldest first (credit review reads it)."""
        w = s.checkin_weeks.c
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(w.week).where(
                w.tenant_id == self.tenant_id, w.shop_id == shop_id,
                w.status == "answered").order_by(w.week)).all()
        return [r.week for r in rows]


@dataclass
class _ShopRow:
    engine: Engine
    tenant_id: str

    def _row(self, conn: sa.Connection, shop_id: str) -> Any:
        insert_ignore(conn, s.checkin_shops, {
            "tenant_id": self.tenant_id, "shop_id": shop_id, "skips": 0, "streak": 0,
            "connect_offered": False, "updated_at": now_iso(wall())})
        c = s.checkin_shops.c
        return conn.execute(sa.select(s.checkin_shops).where(
            c.tenant_id == self.tenant_id, c.shop_id == shop_id)).one()

    def _read(self, shop_id: str) -> Any:
        c = s.checkin_shops.c
        with self.engine.connect() as conn:
            return conn.execute(sa.select(s.checkin_shops).where(
                c.tenant_id == self.tenant_id, c.shop_id == shop_id)).first()

    def _set(self, conn: sa.Connection, shop_id: str, **values: Any) -> None:
        c = s.checkin_shops.c
        conn.execute(s.checkin_shops.update().where(
            c.tenant_id == self.tenant_id, c.shop_id == shop_id)
            .values(updated_at=now_iso(wall()), **values))


class SqlSkipLedger(_ShopRow):
    def note_skip(self, shop_id: str) -> int:
        with self.engine.begin() as conn:
            n = int(self._row(conn, shop_id).skips) + 1
            self._set(conn, shop_id, skips=n)
        return n

    def note_answer(self, shop_id: str) -> None:
        with self.engine.begin() as conn:
            self._row(conn, shop_id)
            self._set(conn, shop_id, skips=0)

    def consecutive(self, shop_id: str) -> int:
        r = self._read(shop_id)
        return int(r.skips) if r is not None else 0

    def needs_connect_offer(self, shop_id: str) -> bool:
        r = self._read(shop_id)
        return r is not None and int(r.skips) >= CONNECT_AFTER_SKIPS and not r.connect_offered

    def mark_offered(self, shop_id: str) -> None:
        with self.engine.begin() as conn:
            self._row(conn, shop_id)
            self._set(conn, shop_id, connect_offered=True)

    def weekly_allowed(self, shop_id: str) -> bool:
        return self.consecutive(shop_id) < CONNECT_AFTER_SKIPS


class SqlStreaks(_ShopRow):
    def note_answer(self, shop_id: str) -> int:
        with self.engine.begin() as conn:
            n = int(self._row(conn, shop_id).streak) + 1
            self._set(conn, shop_id, streak=n)
        return n

    def note_skip(self, shop_id: str) -> None:
        with self.engine.begin() as conn:
            self._row(conn, shop_id)
            self._set(conn, shop_id, streak=0)

    def current(self, shop_id: str) -> int:
        r = self._read(shop_id)
        return int(r.streak) if r is not None else 0

    def earns_credit_line(self, shop_id: str) -> bool:
        return self.current(shop_id) >= STREAK_CREDIT_LINE_AFTER


def answered_in_window(engine: Engine, tenant_id: str, shop_id: str, today_iso: str,
                       weeks: int = 8) -> int:
    """Weekly check-ins the shop answered in the last ``weeks`` ISO weeks
    (credit review's sharing evidence). 0 when the table is not there yet."""
    from datetime import date, timedelta

    today = date.fromisoformat(today_iso)
    window = set()
    for i in range(weeks):
        y, w, _ = (today - timedelta(weeks=i)).isocalendar()
        window.add(f"{y}W{w:02d}")
    try:
        got = SqlCheckinStore(engine, tenant_id).answered_weeks(shop_id)
    except (sa.exc.OperationalError, sa.exc.ProgrammingError) as exc:
        # The table is not there yet (before alembic 0015): no evidence. Any
        # other store failure is raised, not read as "never answered".
        log.warning("checkin_weeks unreadable, no sharing evidence: %s", exc.__class__.__name__)
        return 0
    return sum(1 for w in got if w in window)


__all__ = ["SqlCheckinStore", "SqlSkipLedger", "SqlStreaks", "answered_in_window"]
