"""The day's model allowance: N250 per active shop, fixed at Lagos midnight.

The owner's decision (30 Oct 2026) is that a distributor's day is budgeted by
the number of shops actually doing business, not by a flat number and not by
the rows in ``shops``:

* ``SHOP_ALLOWANCE_KOBO`` a day per **active shop**, floored at one shop, so a
  tenant with no activity still gets N250 instead of zero (zero looks like a
  bug and would block the first message).
* **Active** means a shop with any recorded activity in the 30 days *before
  today*: a message (through the turn it starts), an order, a payment or a
  return. Dormant rows in ``shops`` buy nothing, so a distributor with 400
  sleeping shops is not handed N100,000 a day.
* The window ends at today's Lagos midnight, so **today's own activity does
  not count**: a shop added or woken mid-day does not raise today's cap. It
  raises tomorrow's, at midnight, when the window moves.
* The day's figure is therefore fixed for the whole day, and this module
  memoises it per store, tenant and day. One function, ``allowance``, is read
  by the gate (``SqlGo.daily_spend``) and by the report
  (``mimi.observe.metrics``), so the number shown and the number enforced
  cannot disagree.
* An approved ``raise_daily_allowance`` for that day is the only thing that
  can lift the cap, and only for that day: it expires with the day it was
  approved in. The tenant column ``daily_cap_kobo`` stays as the audit trail
  of the last raise; the gate does not read it.

Money is integer kobo. The unit the owner reads is model turns
(``spend.turns_for``), derived from the measured average in ``COST.md``; the
limit enforced is money, because a vision turn costs many times a text turn
and a turn count would overspend a money budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import sqlalchemy as sa

from mimi.observe import spend as budget
from mimi.tz import LAGOS

# How far back a shop's activity makes it active.
ACTIVE_WINDOW_DAYS = 30
# The window is at most one process-wide cache entry per tenant per day.
_MEMO: dict[tuple[int, str, str], int] = {}
_MEMO_MAX = 1024


@dataclass(frozen=True)
class Allowance:
    """One tenant's day: how many shops count, what it buys, what it shows."""

    tenant_id: str
    day: date
    active_shops: int
    computed_kobo: int
    raised_kobo: int
    cap_kobo: int
    window_start: datetime
    window_end: datetime

    @property
    def turns(self) -> int:
        """M, the cap in model turns, derived from the measured average cost."""
        return budget.turns_for(self.cap_kobo)

    @property
    def source(self) -> str:
        """"shops" when the shop count set the cap, "raised" when the owner did."""
        return "raised" if self.raised_kobo > self.computed_kobo else "shops"


def window(day: date) -> tuple[datetime, datetime]:
    """The 30 days before ``day``'s Lagos midnight, as one half-open range."""
    start = datetime.combine(day - timedelta(days=ACTIVE_WINDOW_DAYS), time.min,
                             tzinfo=LAGOS)
    end = datetime.combine(day, time.min, tzinfo=LAGOS)
    return start, end


_ACTIVE_SQL = sa.text(
    "SELECT COUNT(*) FROM ("
    " SELECT th.shop_id AS shop_id FROM threads th"
    "  JOIN turns tu ON tu.tenant_id = th.tenant_id AND tu.thread_id = th.thread_id"
    "  WHERE th.tenant_id = :tenant AND th.kind = 'shop' AND th.shop_id <> ''"
    "   AND tu.created_ts >= :start AND tu.created_ts < :end"
    " UNION SELECT d.shop_id AS shop_id FROM deliveries d"
    "  WHERE d.tenant_id = :tenant AND d.shop_id <> ''"
    "   AND d.delivered_on >= :from_day AND d.delivered_on < :to_day"
    " UNION SELECT p.shop_id AS shop_id FROM payments p"
    "  WHERE p.tenant_id = :tenant AND p.shop_id <> ''"
    "   AND p.paid_on >= :from_day AND p.paid_on < :to_day"
    " UNION SELECT r.shop_id AS shop_id FROM returns r"
    "  WHERE r.tenant_id = :tenant AND r.shop_id <> ''"
    "   AND r.created_ts >= :start AND r.created_ts < :end"
    ") AS active"
)


def active_shops(conn: Any, tenant_id: str, day: date) -> int:
    """Shops with any activity in the 30 days before ``day`` (Lagos midnight).

    A message counts through the turn it starts: every inbound message creates
    one, so ``turns`` is the message source and there is nothing to double
    count. Read-only, one query, memoised for the day.
    """
    key = (id(getattr(conn, "engine", conn)), tenant_id, day.isoformat())
    hit = _MEMO.get(key)
    if hit is not None:
        return hit
    start, end = window(day)
    rooms: dict[str, Any] = {
        "tenant": tenant_id,
        "start": start.timestamp(),
        "end": end.timestamp(),
        "from_day": start.date().isoformat(),
        "to_day": end.date().isoformat(),
    }
    count = int(conn.execute(_ACTIVE_SQL, rooms).scalar() or 0)
    if len(_MEMO) > _MEMO_MAX:  # a long-lived process, many stores in tests
        _MEMO.clear()
    _MEMO[key] = count
    return count


def raised_today(conn: Any, tenant_id: str, day: date) -> int:
    """The largest raise the owner approved for ``day``; zero when none."""
    rows = conn.execute(sa.text(
        "SELECT payload FROM approvals WHERE tenant_id = :tenant"
        " AND tool = 'raise_daily_allowance' AND status = 'approved'"
    ), {"tenant": tenant_id}).fetchall()
    best = 0
    for row in rows:
        payload = _payload(row[0])
        if str(payload.get("day") or "") != day.isoformat():
            continue
        best = max(best, int(payload.get("new_cap_kobo") or 0))
    return best


def _payload(raw: Any) -> dict[str, Any]:
    import json

    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def allowance(conn: Any, tenant_id: str, day: date) -> Allowance:
    """The tenant's allowance for ``day``: active shops, the cap, the turns."""
    shops = active_shops(conn, tenant_id, day)
    computed = budget.shop_allowance(shops)
    raised = raised_today(conn, tenant_id, day)
    start, end = window(day)
    return Allowance(tenant_id=tenant_id, day=day, active_shops=shops,
                     computed_kobo=computed, raised_kobo=raised,
                     cap_kobo=max(computed, raised), window_start=start, window_end=end)


def forget() -> None:
    """Drop the memo (a test, or a store rebuilt under the same tenant ids)."""
    _MEMO.clear()


__all__ = ["ACTIVE_WINDOW_DAYS", "Allowance", "active_shops", "allowance", "forget",
           "raised_today", "window"]
