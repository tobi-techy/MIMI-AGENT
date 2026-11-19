"""The daily attention list: cycles missed, holes on top, money to the kobo.

Every test imports a real book through ``mimi.connectors.book`` and reads it with
``mimi.store.attention``: the same two paths production uses. The numbers are
hand counts written out in the test, so a wrong sum fails here rather than
looking plausible on a screen.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from mimi import clock
from mimi.connectors.book import import_book
from mimi.store import schema as s
from mimi.store.attention import attention, main, render, today
from mimi.store.db import ensure_schema, make_engine
from mimi.tz import LAGOS

FIXTURES = Path(__file__).parent / "fixtures" / "books" / "small" / "csv"
TENANT = "sunline-dist"
BOOK_DAY = date(2026, 10, 27)

DISTRIBUTOR = ["tenant_id", "name", "owner_phones"]
REPS = ["rep_id", "name", "phone", "areas"]
SHOPS = ["shop_id", "name", "owner", "area", "street", "phone", "rep_id",
         "credit_limit_naira", "restock_days"]
SKUS = ["sku", "name", "brand", "unit", "price_naira"]
DELIVERIES = ["delivery_id", "shop_id", "rep_id", "delivered_on", "minute", "total_naira"]
LINES = ["delivery_id", "sku", "quantity", "unit_price_naira", "batch", "expires_on"]
RECEIVABLES = ["delivery_id", "shop_id", "due_on", "total_naira", "paid_naira"]
PAYMENTS = ["payment_id", "shop_id", "delivery_id", "paid_on", "amount_naira", "method",
            "reference"]


def engine(tmp_path: Path) -> sa.Engine:
    made = make_engine(f"sqlite:///{tmp_path / 'book.db'}")
    ensure_schema(made)
    return made


def write_book(tmp_path: Path, *, shops: list[list[str]], deliveries: list[list[str]] = (),
               lines: list[list[str]] = (), receivables: list[list[str]] = (),
               payments: list[list[str]] = ()) -> Path:
    """A book of the sizes a test needs, written as the CSVs the importer reads."""
    tables: dict[str, list[list[Any]]] = {
        "distributor": [DISTRIBUTOR, [TENANT, "Sunline Distributors", "+2340000000101"]],
        "reps": [REPS, ["rep-01", "Abiola Nwosu", "+2340000000201", "Ikeja"]],
        "shops": [SHOPS, *shops],
        "skus": [SKUS, ["SKU-001", "Golden Soya Oil 5L", "Golden", "carton", "18500.00"]],
        "deliveries": [DELIVERIES, *deliveries],
        "delivery_lines": [LINES, *lines],
        "receivables": [RECEIVABLES, *receivables],
        "payments": [PAYMENTS, *payments],
    }
    folder = tmp_path / "book"
    folder.mkdir()
    for table, grid in tables.items():
        with (folder / f"{table}.csv").open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(grid)
    return folder


def shop(shop_id: str, name: str, restock_days: str, limit_naira: str = "500000.00",
         area: str = "Ikeja") -> list[str]:
    return [shop_id, name, "Owner", area, "1 Test Street", "+2340000000301", "rep-01",
            limit_naira, restock_days]


def delivery(delivery_id: str, shop_id: str, delivered_on: str) -> list[str]:
    return [delivery_id, shop_id, "rep-01", delivered_on, "540", "15000.00"]


def line(delivery_id: str) -> list[str]:
    return [delivery_id, "SKU-001", "1", "15000.00", "", ""]


# --------------------------------------------------------------- the real book


def test_the_fixture_book_lists_the_shop_that_is_late(tmp_path: Path) -> None:
    """On 27 Oct 2026 shop-001 is 11 days from a 7 day rhythm (one cycle missed)
    and owes on two deliveries. shop-002 was delivered that day and shop-003 is
    6 days into a 14 day rhythm, so neither is an exception."""
    db = engine(tmp_path)
    import_book(db, FIXTURES, tenant_id=TENANT)

    rows = attention(db, TENANT, BOOK_DAY)

    assert [r.shop_id for r in rows] == ["shop-001"]
    row = rows[0]
    assert row.kind == "overdue"
    assert (row.cycles_missed, row.restock_days, row.days_since_last_delivery) == (1, 7, 11)
    assert row.shop_name == "Mama Tope Stores" and row.area == "Ikeja"
    assert row.rep_id == "rep-01"
    assert row.over_limit is False


def test_the_amounts_are_a_hand_count_to_the_kobo(tmp_path: Path) -> None:
    """shop-001's money, counted by hand from the four receivable rows: dlv-0001
    58,002.50 less 15,000.00 paid and due 09 Oct (late on the 27th), dlv-0004
    39,200.00 unpaid and due 30 Oct (not late yet)."""
    outstanding = (5_800_250 - 1_500_000) + (3_920_000 - 0)
    overdue = 5_800_250 - 1_500_000
    assert (outstanding, overdue) == (8_220_250, 4_300_250)

    db = engine(tmp_path)
    import_book(db, FIXTURES, tenant_id=TENANT)
    [row] = attention(db, TENANT, BOOK_DAY)

    assert row.outstanding_kobo == outstanding
    assert row.overdue_kobo == overdue
    # ₦150,000.00 of credit granted less the ₦82,202.50 owed.
    assert row.credit_position_kobo == 15_000_000 - outstanding


def test_a_shop_inside_its_cycle_is_not_listed(tmp_path: Path) -> None:
    db = engine(tmp_path)
    import_book(db, FIXTURES, tenant_id=TENANT)
    listed = {r.shop_id for r in attention(db, TENANT, BOOK_DAY)}
    assert listed == {"shop-001"}
    # A week later shop-002 (last in on the 27th) is still inside its 7 days and
    # shop-003 is inside its 14, so the same one shop is still the exception.
    assert {r.shop_id for r in attention(db, TENANT, date(2026, 11, 3))} == {
        "shop-001", "shop-002"}


def test_the_default_day_is_the_books_own_day(tmp_path: Path) -> None:
    """An imported book cut off on 27 Oct is counted from 27 Oct, not from the
    machine's calendar: otherwise this list and every other surface measure the
    same book from two different days."""
    db = engine(tmp_path)
    import_book(db, FIXTURES, tenant_id=TENANT)

    default = attention(db, TENANT)

    assert default == attention(db, TENANT, BOOK_DAY)
    [row] = default
    assert row.days_since_last_delivery == 11
    assert today() != BOOK_DAY


def test_a_book_that_names_no_day_falls_back_to_the_lagos_clock(tmp_path: Path) -> None:
    db = engine(tmp_path)
    import_book(db, FIXTURES, tenant_id=TENANT)
    with db.begin() as conn:
        conn.execute(sa.update(s.tenants).values(live_date=""))
    pinned = datetime(2026, 12, 1, 9, 0, tzinfo=LAGOS).timestamp()
    previous = clock.set_source(lambda: pinned)
    try:
        rows = attention(db, TENANT)
    finally:
        clock.set_source(previous)
    # shop-001 was last delivered on 16 Oct, so 1 Dec on its 7 day rhythm is 46
    # days and 6 cycles, which is not what the book's own day would say.
    [row] = [r for r in rows if r.shop_id == "shop-001"]
    assert row.days_since_last_delivery == 46
    assert row.cycles_missed == 6


# ------------------------------------------------------- the order that matters


def test_a_two_day_shop_outranks_a_weekly_shop_at_the_same_days(tmp_path: Path) -> None:
    """Both nine days from a delivery. Four cycles missed against one, so the
    two-day shop is the emergency: ordering by days would put them level."""
    book = write_book(
        tmp_path,
        shops=[shop("shop-004", "Quick Corner", "2"), shop("shop-005", "Weekly Store", "7")],
        deliveries=[delivery("dlv-1001", "shop-004", "2026-10-18"),
                    delivery("dlv-1002", "shop-005", "2026-10-18")],
        lines=[line("dlv-1001"), line("dlv-1002")])
    db = engine(tmp_path)
    import_book(db, book, tenant_id=TENANT)

    rows = attention(db, TENANT, BOOK_DAY)

    assert [r.shop_id for r in rows] == ["shop-004", "shop-005"]
    assert [r.days_since_last_delivery for r in rows] == [9, 9]
    assert [r.cycles_missed for r in rows] == [4, 1]


def test_outstanding_breaks_a_tie_and_never_starts_a_row(tmp_path: Path) -> None:
    """Two shops on the same rhythm with the same days: the bigger debt is
    first. The third shop owes more than either and is not late, so it is not
    on the list at all."""
    book = write_book(
        tmp_path,
        shops=[shop("shop-004", "Small Debt", "7"), shop("shop-005", "Big Debt", "7"),
               shop("shop-006", "Paid Up But Late", "14")],
        deliveries=[delivery("dlv-1001", "shop-004", "2026-10-17"),
                    delivery("dlv-1002", "shop-005", "2026-10-17"),
                    delivery("dlv-1003", "shop-006", "2026-10-20")],
        lines=[line("dlv-1001"), line("dlv-1002"), line("dlv-1003")],
        receivables=[["dlv-1001", "shop-004", "2026-10-20", "15000.00", "14000.00"],
                     ["dlv-1002", "shop-005", "2026-10-20", "15000.00", "1000.00"],
                     ["dlv-1003", "shop-006", "2026-12-01", "15000.00", "0.00"]],
        payments=[["pay-1001", "shop-004", "dlv-1001", "2026-10-20", "14000.00", "cash", ""],
                  ["pay-1002", "shop-005", "dlv-1002", "2026-10-20", "1000.00", "cash", ""]])
    db = engine(tmp_path)
    import_book(db, book, tenant_id=TENANT)

    rows = attention(db, TENANT, BOOK_DAY)

    assert [r.shop_id for r in rows] == ["shop-005", "shop-004"]
    assert [r.outstanding_kobo for r in rows] == [1_400_000, 100_000]


# ----------------------------------------------------------- a hole is not zero


def test_a_shop_with_no_delivery_is_unknown_and_sorts_on_top(tmp_path: Path) -> None:
    book = write_book(
        tmp_path,
        shops=[shop("shop-004", "Never Visited", "7"), shop("shop-005", "Long Overdue", "7")],
        deliveries=[delivery("dlv-1001", "shop-005", "2026-09-20")],
        lines=[line("dlv-1001")])
    db = engine(tmp_path)
    import_book(db, book, tenant_id=TENANT)

    rows = attention(db, TENANT, BOOK_DAY)

    assert [r.shop_id for r in rows] == ["shop-004", "shop-005"]
    hole, measured = rows
    assert hole.kind == "unknown"
    assert hole.cycles_missed is None and hole.days_since_last_delivery is None
    assert measured.kind == "overdue" and measured.cycles_missed == 5


def test_a_zero_restock_interval_is_unknown_not_a_division_by_zero(tmp_path: Path) -> None:
    """A zero interval is a hole in the book, not a shop that needs a call every
    day, and it cannot be ranked against shops that have a rhythm."""
    book = write_book(
        tmp_path,
        shops=[shop("shop-004", "No Rhythm", "0"), shop("shop-005", "Long Overdue", "7")],
        deliveries=[delivery("dlv-1001", "shop-005", "2026-09-20")],
        lines=[line("dlv-1001")])
    db = engine(tmp_path)
    import_book(db, book, tenant_id=TENANT)

    rows = attention(db, TENANT, date(2026, 10, 27))

    assert [r.shop_id for r in rows] == ["shop-004", "shop-005"]
    hole = rows[0]
    assert hole.kind == "unknown" and hole.cycles_missed is None
    assert hole.restock_days is None
    assert hole.days_since_last_delivery is None


def test_an_interval_hole_still_reports_the_days_it_cannot_rank(tmp_path: Path) -> None:
    """No interval but a delivery on record: the row says the days since, and
    still carries no cycles number, because days alone rank nothing."""
    book = write_book(
        tmp_path,
        shops=[shop("shop-004", "No Rhythm", "0")],
        deliveries=[delivery("dlv-1001", "shop-004", "2026-10-20")],
        lines=[line("dlv-1001")])
    db = engine(tmp_path)
    import_book(db, book, tenant_id=TENANT)

    [row] = attention(db, TENANT, BOOK_DAY)

    assert (row.kind, row.restock_days, row.days_since_last_delivery) == (
        "unknown", None, 7)
    assert row.cycles_missed is None


def test_a_delivery_after_the_day_counted_is_a_hole_and_not_an_absence(tmp_path: Path) -> None:
    """Counting an imported book from an earlier day than its newest delivery:
    that delivery has not happened as of the day, so there is nothing to measure
    from. The shop is a hole for that day, never a missing row, and the line
    must not claim the book has no delivery for it."""
    book = write_book(
        tmp_path,
        shops=[shop("shop-004", "Delivered Later", "7"), shop("shop-005", "Nothing Yet", "7")],
        deliveries=[delivery("dlv-1001", "shop-004", "2026-11-05")],
        lines=[line("dlv-1001")])
    db = engine(tmp_path)
    import_book(db, book, tenant_id=TENANT)

    rows = attention(db, TENANT, BOOK_DAY)

    assert [r.shop_id for r in rows] == ["shop-004", "shop-005"]
    later, nothing = rows
    for row in rows:
        assert (row.kind, row.cycles_missed, row.days_since_last_delivery) == (
            "unknown", None, None)
    text = render(rows, BOOK_DAY)
    assert text.count("nothing delivered by this day") == 2
    assert "no delivery on record" not in text


def test_days_read_in_the_singular_and_the_plural(tmp_path: Path) -> None:
    """A shop with no rhythm but a delivery on record is the one hole that still
    knows its days, so it is what shows the wording: today, one day, two days."""
    book = write_book(
        tmp_path,
        shops=[shop("shop-004", "Today Shop", "0"), shop("shop-005", "One Day Shop", "0"),
               shop("shop-006", "Two Day Shop", "0")],
        deliveries=[delivery("dlv-1001", "shop-004", "2026-10-27"),
                    delivery("dlv-1002", "shop-005", "2026-10-26"),
                    delivery("dlv-1003", "shop-006", "2026-10-25")],
        lines=[line("dlv-1001"), line("dlv-1002"), line("dlv-1003")])
    db = engine(tmp_path)
    import_book(db, book, tenant_id=TENANT)

    text = render(attention(db, TENANT, BOOK_DAY), BOOK_DAY)

    assert "delivered today" in text
    assert "nothing in 1 day," in text
    assert "nothing in 2 days," in text
    assert "1 days" not in text


# -------------------------------------------------------------------- the limit


def test_a_shop_over_its_credit_limit_is_flagged(tmp_path: Path) -> None:
    book = write_book(
        tmp_path,
        shops=[shop("shop-004", "Over Limit", "7", limit_naira="5000.00")],
        deliveries=[delivery("dlv-1001", "shop-004", "2026-10-01")],
        lines=[line("dlv-1001")],
        receivables=[["dlv-1001", "shop-004", "2026-10-08", "15000.00", "5000.00"]],
        payments=[["pay-1001", "shop-004", "dlv-1001", "2026-10-08", "5000.00", "cash", ""]])
    db = engine(tmp_path)
    import_book(db, book, tenant_id=TENANT)

    [row] = attention(db, TENANT, BOOK_DAY)

    assert row.outstanding_kobo == 1_000_000  # ₦10,000.00 owed
    assert row.credit_position_kobo == 500_000 - 1_000_000  # limit ₦5,000.00
    assert row.over_limit is True
    assert "₦5,000 over its ₦5,000 limit" in render([row], BOOK_DAY)


# ------------------------------------------------------------- empty, and the CLI


def test_an_empty_book_is_an_empty_list_not_an_error(tmp_path: Path) -> None:
    db = engine(tmp_path)
    assert attention(db, TENANT, BOOK_DAY) == []
    assert render([], BOOK_DAY) == "No shop needs attention today (27 Oct 2026)."


def test_the_operator_runs_one_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = engine(tmp_path)
    import_book(db, FIXTURES, tenant_id=TENANT)
    url = f"sqlite:///{tmp_path / 'book.db'}"

    assert main(["--tenant-id", TENANT, "--db", url, "--day", "2026-10-27"]) == 0
    listed = capsys.readouterr().out
    assert listed == (
        "1 shop needs attention today (27 Oct 2026).\n"
        "\n"
        "Past their own restock cycle, most cycles missed first:\n"
        "  Mama Tope Stores, Ikeja, rep-01: restocks every 7 days, nothing in 11 days, "
        "1 cycle missed, ₦82,202.50 outstanding, ₦43,002.50 of it overdue.\n")

    # Without --day the command counts from the book's own day, so it prints the
    # same list rather than one measured from the machine's calendar.
    assert main(["--tenant-id", TENANT, "--db", url]) == 0
    assert capsys.readouterr().out == listed

    assert main(["--tenant-id", TENANT, "--db", url, "--day", "2026-10-27", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed[0]["shop_id"] == "shop-001"
    assert printed[0]["kind"] == "overdue" and printed[0]["cycles_missed"] == 1
    assert printed[0]["outstanding_kobo"] == 8_220_250


def test_a_tenant_the_book_does_not_have_is_an_error(tmp_path: Path,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    """An empty book reads as empty, but a mistyped id must not: the operator
    would believe a calm book."""
    db = engine(tmp_path)
    import_book(db, FIXTURES, tenant_id=TENANT)
    url = f"sqlite:///{tmp_path / 'book.db'}"

    assert main(["--tenant-id", "sunline-distt", "--db", url]) == 2
    assert "no tenant 'sunline-distt' in this book" in capsys.readouterr().err
