"""The day's allowance: N250 per active shop, fixed at Lagos midnight.

The owner's rule has four parts, and each one is a test here:

* the cap is N250 a day per active shop, floored at one shop, so a tenant with
  no activity gets N250 and never zero;
* an active shop is one with a message, turn, order, payment or return in the
  30 days before today, so dormant rows in ``shops`` buy nothing;
* the window ends at today's Lagos midnight, so today's own activity (and a
  shop added mid-day) does not raise today's cap, and midnight does;
* an approved raise lifts the cap for its own day only.

Money is the enforced limit; turns are the derived unit the owner reads.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from mimi.observe import allowance
from mimi.observe import spend as budget
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.store.book import SqlGo
from mimi.world.loader import load_world

W = load_world()
T = "coral-lane"
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # 10:00 WAT
DAY = date(2026, 10, 26)
TOMORROW = date(2026, 10, 27)


def fresh() -> Any:
    engine = make_engine("sqlite://")
    ensure_schema(engine)
    seed_world(engine, W)
    allowance.forget()
    return engine


def cap_now(engine: Any, tenant: str = T, day: date = DAY) -> int:
    allowance.forget()
    with engine.connect() as conn:
        return allowance.allowance(conn, tenant, day).cap_kobo


# ---------------------------------------------------------------- arithmetic


def test_the_cap_is_n250_a_shop_with_a_one_shop_floor() -> None:
    assert budget.SHOP_ALLOWANCE_KOBO == 25_000
    assert budget.shop_allowance(0) == budget.shop_allowance(1) == 25_000
    assert budget.shop_allowance(8) == 200_000
    assert budget.shop_allowance(40) == 1_000_000


def test_the_founder_cases_in_model_turns() -> None:
    assert budget.turns_for(budget.shop_allowance(1)) == 15
    assert budget.turns_for(budget.shop_allowance(8)) == 121
    assert budget.turns_for(budget.shop_allowance(40)) == 609


def test_money_is_the_limit_and_turns_are_the_view() -> None:
    """A vision turn costs many times a text turn: money is what stops it."""
    cap = budget.shop_allowance(8)
    assert budget.turns_for(cap) * budget.AVG_TURN_KOBO <= cap
    assert (budget.turns_for(cap) + 1) * budget.AVG_TURN_KOBO > cap


# ------------------------------------------------------------------- counting


def test_activity_in_the_thirty_days_before_today_counts() -> None:
    engine = fresh()
    allowance.forget()
    with engine.connect() as conn:
        got = allowance.allowance(conn, T, DAY)
        # Independent check: the distinct shops with any row in the window.
        start, end = allowance.window(DAY)
        shops = {r.shop_id for r in conn.execute(sa.text(
            "SELECT shop_id FROM deliveries WHERE tenant_id = :t"
            " AND delivered_on >= :a AND delivered_on < :b"),
            {"t": T, "a": start.date().isoformat(), "b": end.date().isoformat()})}
        shops |= {r.shop_id for r in conn.execute(sa.text(
            "SELECT shop_id FROM payments WHERE tenant_id = :t"
            " AND paid_on >= :a AND paid_on < :b"),
            {"t": T, "a": start.date().isoformat(), "b": end.date().isoformat()})}
    assert got.active_shops == len(shops) >= 1
    assert got.cap_kobo == 25_000 * got.active_shops


def test_a_tenant_with_no_activity_gets_the_floor_not_zero() -> None:
    engine = fresh()
    assert cap_now(engine, "nobody-inc") == 25_000


def test_dormant_shop_rows_buy_nothing() -> None:
    """400 shops in a table and no activity is N250, not N100,000."""
    engine = fresh()
    before = cap_now(engine, "dormant-lane")
    with engine.begin() as conn:
        for n in range(20):
            conn.execute(sa.insert(s.shops).values(
                tenant_id="dormant-lane", shop_id=f"shop-{n:03d}",
                name=f"Dormant {n}", phone="+2348030000000", area="Yaba"))
    assert cap_now(engine, "dormant-lane") == before == 25_000


# ------------------------------------------------------------------ the clock


def test_todays_own_activity_does_not_raise_todays_cap() -> None:
    engine = fresh()
    before = cap_now(engine)
    with engine.begin() as conn:  # an order dated today, for a shop not seen before
        conn.execute(sa.insert(s.deliveries).values(
            tenant_id=T, delivery_id="dlv-today", shop_id="shop-new-today",
            delivered_on=DAY.isoformat(), day=int(DAY.strftime("%Y%m%d")),
            total_kobo=100_000, status="delivered"))
    assert cap_now(engine) == before, "a shop added mid-day does not raise today's cap"
    assert cap_now(engine, day=TOMORROW) == before + 25_000, "midnight moves the window"


def test_the_window_is_thirty_days_to_the_minute_it_starts() -> None:
    start, end = allowance.window(DAY)
    assert end.date() == DAY and end.hour == 0 and end.minute == 0
    assert (end - start).days == allowance.ACTIVE_WINDOW_DAYS == 30


# ------------------------------------------------------------------ the raise


def test_an_approved_raise_lifts_the_cap_for_that_day_only() -> None:
    engine = fresh()
    computed = cap_now(engine)
    with engine.begin() as conn:
        for day, status, cap in ((DAY, "approved", 300_000),
                                 (DAY - timedelta(days=1), "approved", 900_000),
                                 (DAY, "pending", 800_000)):
            conn.execute(sa.insert(s.approvals).values(
                approval_id=f"apr-{day}-{status}", tenant_id=T,
                idempotency_key=f"k-{day}-{status}", turn_id="", tool="raise_daily_allowance",
                intent_kind="raise_daily_allowance", status=status,
                payload=json.dumps({"day": day.isoformat(), "cap_kobo": computed,
                                    "new_cap_kobo": cap}),
                created_at=NOW.isoformat(), created_ts=NOW.timestamp()))
    assert cap_now(engine) == max(computed, 300_000)
    assert cap_now(engine, day=DAY + timedelta(days=1)) == 25_000 * (
        active_shops_on(engine, DAY + timedelta(days=1)))


def active_shops_on(engine: Any, day: date) -> int:
    allowance.forget()
    with engine.connect() as conn:
        return allowance.allowance(conn, T, day).active_shops


# ------------------------------------------------------------ one number, two readers


def test_the_gate_and_the_report_read_the_same_number() -> None:
    from mimi.observe import metrics

    engine = fresh()
    with engine.begin() as conn:  # a little spend, so the row is worth showing
        conn.execute(sa.insert(s.spend).values(tenant_id=T, day=DAY.isoformat(), kobo=10_000))
    _, by_gate = SqlGo(engine).daily_spend(T, DAY)
    snap = metrics.snapshot(engine, now=NOW)
    row = next(r for r in snap["spend"] if r["tenant_id"] == T)
    assert by_gate == row["cap_kobo"] == cap_now(engine)
    assert row["turns_cap"] == budget.turns_for(by_gate)
    assert row["turns_used"] == budget.turns_for(10_000)
    assert row["active_shops"] == active_shops_on(engine, DAY)
