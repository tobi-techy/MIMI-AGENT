"""The daily brief (MIAG-10): the message, its memory, and the 8:30 variance.

The eval (``mimi.api.brief_eval``) marks what the message says over imported
books. These tests cover the parts an eval of the text cannot: the snapshot the
brief keeps for tomorrow, the per-rep variance the 8:30 scan posts against the
morning's targets, the style guard, and the unhappy paths (an unknown tenant, a
day with no brief behind it).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from mimi import owed
from mimi.api import brief as bf
from mimi.api import routines as rt
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.tz import LAGOS
from mimi.world.loader import load_world

W = load_world()
T = W.tenant_id
AT = datetime(2026, 11, 4, 8, 0, tzinfo=LAGOS)


@pytest.fixture()
def engine(store_url: str) -> Iterator[sa.Engine]:
    made = make_engine(store_url)
    ensure_schema(made)
    seed_world(made, W)
    yield made
    made.dispose()


def _snapshot(engine: sa.Engine) -> Any:
    with engine.connect() as conn:
        return conn.execute(sa.select(s.brief_snapshots).where(
            s.brief_snapshots.c.tenant_id == T)).first()


def test_the_brief_is_one_message_with_five_sections_and_named_owners(
        engine: sa.Engine) -> None:
    built = bf.build(engine, T, AT.date())
    assert built.text.startswith("Mimi morning brief,")
    heads = ["What changed:", "Money at risk:", "Collections today:", "Returns:", "Coverage:"]
    positions = [built.text.index(head) for head in heads]
    assert positions == sorted(positions)
    assert len(built.items) <= bf.ITEM_CAP
    # Every item names an owner and an action: a person, or the owner reading it.
    for item in built.items:
        assert item.text.rstrip().endswith(".") and (
            "Rep " in item.text or "You:" in item.text), item.text
    assert "\u2014" not in built.text and "\u2013" not in built.text


def test_an_unknown_tenant_is_refused_rather_than_read_as_a_calm_book(
        engine: sa.Engine) -> None:
    with pytest.raises(LookupError):
        bf.build(engine, "nobody", AT.date())


def test_the_brief_keeps_its_own_numbers_and_only_once_a_day(engine: sa.Engine) -> None:
    built = bf.build(engine, T, AT.date())
    bf.record(engine, T, built, AT)
    first = _snapshot(engine)
    assert first is not None
    assert first.day == AT.date().isoformat()
    assert first.book_day == built.book_day.isoformat()
    assert int(first.at_risk_kobo) == int(built.snapshot["at_risk_kobo"])
    assert int(first.items_listed) == len(built.items)
    # A second run the same day writes nothing over it: the day's row is final.
    again = bf.build(engine, T, AT.date())
    bf.record(engine, T, again, AT.replace(hour=9))
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(s.brief_snapshots)
                            .where(s.brief_snapshots.c.tenant_id == T)).scalar() == 1


def test_tomorrow_leads_with_the_movement(engine: sa.Engine) -> None:
    """The first brief says there is nothing to compare; the next one says what
    moved, from the snapshot rather than from an assumption."""
    bf.record(engine, T, bf.build(engine, T, AT.date()), AT)
    tomorrow = AT + timedelta(days=1)
    built = bf.build(engine, T, tomorrow.date())
    assert "this is the first brief" not in built.text
    assert "What changed: money at risk is" in built.text
    assert "came in" in built.text


def test_the_variance_is_per_rep_against_the_morning_targets(engine: sa.Engine) -> None:
    """The 8:30 scan says what came in against what each rep was set."""
    built = bf.build(engine, T, AT.date())
    bf.record(engine, T, built, AT)
    assert built.targets, "the morning must have set at least one collection target"
    target = built.targets[0]
    with engine.begin() as conn:
        shop = conn.execute(sa.select(s.shops.c.name).where(
            s.shops.c.tenant_id == T, s.shops.c.shop_id == target["shop_id"])).scalar()
        conn.execute(sa.insert(s.payments).values(
            tenant_id=T, payment_id="pay-variance-1", shop_id=target["shop_id"],
            delivery_id="", day=0, paid_on=built.book_day.isoformat(),
            amount_kobo=500_000, method="transfer", reference="", status="reported",
            partial=False, split_of=None, fake_alert=False))
    words = bf.variance(engine, T, AT.date())
    assert words.startswith("Collections: Rep ")
    assert "was set" in words and "₦5,000 came in" in words
    assert str(shop) in built.text


def test_a_day_with_no_brief_says_it_cannot_be_measured(engine: sa.Engine) -> None:
    assert bf.variance(engine, T, (AT + timedelta(days=3)).date()).startswith(
        "No collection target was set this morning")


def test_the_eight_thirty_scan_posts_the_variance_first(engine: sa.Engine) -> None:
    rt.run_due(engine, T, now=AT)
    rt.run_due(engine, T, now=AT.replace(hour=8, minute=30))
    with engine.connect() as conn:
        text = conn.execute(sa.select(s.messages.c.text).where(
            s.messages.c.tenant_id == T, s.messages.c.kind == "aging_scan")).scalar()
    assert str(text).startswith("Collections:")
    assert "was set" in str(text) and "came in" in str(text)


def test_the_brief_names_no_internal_id(engine: sa.Engine) -> None:
    text = bf.build(engine, T, AT.date()).text
    assert "shop-" not in text and "dlv-" not in text and "ret_" not in text
    assert "_kobo" not in text


def test_the_message_describes_the_book_it_read_not_the_machine_calendar(
        engine: sa.Engine) -> None:
    """The day counted is the book's own (``tenants.live_date``), however far the
    machine's calendar has moved: an owner reading a Friday export on Sunday
    still gets Friday's ages."""
    with engine.connect() as conn:
        live = str(conn.execute(sa.select(s.tenants.c.live_date).where(
            s.tenants.c.tenant_id == T)).scalar())
    built = bf.build(engine, T, date(2026, 12, 25))
    assert built.book_day.isoformat() == live
    assert f"{date.fromisoformat(live).year}" in built.text
    assert owed.naira(int(built.snapshot["at_risk_kobo"])) in built.text
