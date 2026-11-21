"""The book import: every rule, and every way it must refuse.

The fixture book in ``tests/fixtures/books/small`` is a small whole book (3 shops,
2 reps, 4 SKUs, 6 deliveries with 9 lines, 4 receivables, 3 payments). Each
unhappy-path test here takes that book and breaks one thing, then checks the
import refuses exactly that row with a reason and writes nothing wrong. The last
tests are the acceptance ones: import the book, count overdue in code, and
compare it with a hand count of the same rows, then import it twice and require
the book to be unchanged.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from mimi import owed
from mimi.api import reads
from mimi.connectors.base import ConnectorError
from mimi.connectors.book import build_plan, import_book, read_book, write_plan
from mimi.connectors.book.__main__ import main as cli
from mimi.connectors.book.rows import (
    BAD_DATE,
    BAD_MONEY,
    BAD_PHONE,
    DUP_IN_BOOK,
    DUP_IN_FILE,
    MISSING_FIELD,
    MIXES_TWO_SHOPS,
    NO_LINES,
    TOTAL_MISMATCH,
    UNKNOWN_SHOP,
    UNKNOWN_SKU,
)
from mimi.provenance import RECORD_TYPES
from mimi.store import schema as s
from mimi.store.book import SqlGo
from mimi.store.db import ensure_schema, make_engine
from mimi.store.seed import seed_world
from mimi.world.loader import load_world

FIXTURES = Path(__file__).parent / "fixtures" / "books" / "small"
#: The eight tables these tests edit. The fixture book also carries returns, which
#: have rules of their own: ``tests/test_returns_import.py`` reads this same book
#: with them, so breaking one thing here still breaks exactly one thing.
TABLES = ("distributor", "reps", "shops", "skus", "deliveries", "delivery_lines",
          "receivables", "payments")
TENANT = "sunline-dist"
BOOK_TABLES = (s.reps, s.shops, s.skus, s.deliveries, s.delivery_lines, s.receivables,
               s.payments)


# ----------------------------------------------------------------- the harness


def base() -> dict[str, list[list[str]]]:
    """The fixture book, as the tables a test can edit before writing it out."""
    out: dict[str, list[list[str]]] = {}
    for table in TABLES:
        with (FIXTURES / "csv" / f"{table}.csv").open(newline="", encoding="utf-8") as fh:
            out[table] = list(csv.reader(fh))
    return out


def cell(tables: dict[str, list[list[str]]], table: str, row_id: str, column: str,
         value: str) -> dict[str, list[list[str]]]:
    """Change one cell: the row whose first column is ``row_id``, the named column."""
    header = tables[table][0]
    where = header.index(column)
    for row in tables[table][1:]:
        if row[0] == row_id:
            row[where] = value
            return tables
    raise AssertionError(f"{table} has no row {row_id}")


def without(tables: dict[str, list[list[str]]], table: str,
            row_id: str) -> dict[str, list[list[str]]]:
    """Drop every row whose first column is ``row_id`` (a delivery's lines)."""
    keep = [tables[table][0]] + [r for r in tables[table][1:] if r[0] != row_id]
    return {**tables, table: keep}


def write(tables: dict[str, list[list[str]]], folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for table, grid in tables.items():
        with (folder / f"{table}.csv").open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(grid)
    return folder


def book(tmp_path: Path, tables: dict[str, list[list[str]]] | None = None) -> Path:
    return write(tables or base(), tmp_path / "book")


def engine(tmp_path: Path) -> sa.Engine:
    made = make_engine(f"sqlite:///{tmp_path / 'book.db'}")
    ensure_schema(made)
    return made


def rows(db: sa.Engine, table: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sa.select(s.__dict__[table])).mappings()]


def rejects(report: Any, reason: str | None = None,
            entity: str | None = None) -> list[Any]:
    found = [r for r in report.rejects
             if (reason is None or r.reason == reason)
             and (entity is None or r.entity == entity)]
    return found


# ------------------------------------------------------------ file-level refusals


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    """An empty table, or a folder missing one, refuses the file by name: there is
    no honest per-row answer to "this book has no products"."""
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ConnectorError, match="has no distributor.csv"):
        import_book(engine(tmp_path), empty, tenant_id=TENANT)

    tables = base()
    (tmp_path / "blank").mkdir()
    write(tables, tmp_path / "blank")
    (tmp_path / "blank" / "skus.csv").write_text("", encoding="utf-8")
    with pytest.raises(ConnectorError, match="empty"):
        import_book(engine(tmp_path), tmp_path / "blank", tenant_id=TENANT)


def test_a_wrong_header_is_refused(tmp_path: Path) -> None:
    """A missing column refuses the whole file, and the refusal says which column
    and what the header actually read."""
    tables = base()
    tables["skus"][0] = ["sku", "name", "brand", "unit", "price"]
    db = engine(tmp_path)
    with pytest.raises(ConnectorError) as caught:
        import_book(db, book(tmp_path, tables), tenant_id=TENANT)
    assert "price_naira" in str(caught.value)
    assert "price" in caught.value.next_step
    assert rows(db, "shops") == [] and rows(db, "tenants") == []


def test_a_book_naming_another_tenant_is_refused(tmp_path: Path) -> None:
    tables = cell(base(), "distributor", "sunline-dist", "tenant_id", "someone-else")
    with pytest.raises(ConnectorError, match="belongs to 'someone-else'"):
        import_book(engine(tmp_path), book(tmp_path, tables), tenant_id="sunline-dist")


def test_a_single_csv_is_not_a_book(tmp_path: Path) -> None:
    one = tmp_path / "shops.csv"
    one.write_text("shop_id,phone\nshop-001,+2340000000301\n", encoding="utf-8")
    with pytest.raises(ConnectorError, match="one table"):
        import_book(engine(tmp_path), one, tenant_id=TENANT)


# ------------------------------------------------------------- row-level rejects


def test_a_missing_price_is_a_reject_not_a_zero(tmp_path: Path) -> None:
    """A SKU with no price is refused, and so is every delivery that carries it:
    a line whose product has no price cannot produce a total."""
    tables = cell(base(), "skus", "SKU-002", "price_naira", "")
    db = engine(tmp_path)
    report = import_book(db, book(tmp_path, tables), tenant_id=TENANT)

    assert [r.reason for r in rejects(report, entity="skus")] == [MISSING_FIELD]
    assert "price_naira is empty" in rejects(report, entity="skus")[0].detail
    assert {r.reason for r in rejects(report, entity="deliveries")} == {UNKNOWN_SKU}
    assert "SKU-002" in rejects(report, entity="deliveries")[0].detail
    assert [row["sku"] for row in rows(db, "skus")] == ["SKU-001", "SKU-003", "SKU-004"]
    # The deliveries that carry SKU-002 are the refused ones; the rest land.
    assert [row["delivery_id"] for row in rows(db, "deliveries")] == [
        "dlv-0002", "dlv-0004", "dlv-0005"]


def test_an_unparseable_date_rejects_the_delivery(tmp_path: Path) -> None:
    """31 February is not a day. The delivery is refused, never dated to the 28th."""
    tables = cell(base(), "deliveries", "dlv-0003", "delivered_on", "31/02/2026")
    db = engine(tmp_path)
    report = import_book(db, book(tmp_path, tables), tenant_id=TENANT)
    assert [r.reason for r in rejects(report, entity="deliveries")] == [BAD_DATE]
    assert "31/02/2026" in rejects(report, entity="deliveries")[0].detail
    assert "dlv-0003" not in [row["delivery_id"] for row in rows(db, "deliveries")]


def test_a_day_first_date_is_read_as_nigeria_writes_it(tmp_path: Path) -> None:
    """``03/04/2026`` is 3 April: the book's own convention, stated once."""
    tables = cell(base(), "deliveries", "dlv-0003", "delivered_on", "03/04/2026")
    db = engine(tmp_path)
    import_book(db, book(tmp_path, tables), tenant_id=TENANT)
    written = {row["delivery_id"]: row["delivered_on"] for row in rows(db, "deliveries")}
    assert written["dlv-0003"] == "2026-04-03"


def test_a_delivery_with_no_lines_is_refused(tmp_path: Path) -> None:
    """A delivery with nothing on it has no total, and its receivable has nothing
    to be owed on."""
    tables = without(base(), "delivery_lines", "dlv-0004")
    db = engine(tmp_path)
    report = import_book(db, book(tmp_path, tables), tenant_id=TENANT)
    assert [r.reason for r in rejects(report, entity="deliveries")] == [NO_LINES]
    assert [r.reason for r in rejects(report, entity="receivables")] == ["unknown_delivery"]
    assert "dlv-0004" not in [row["delivery_id"] for row in rows(db, "deliveries")]


def test_an_unknown_shop_rejects_the_delivery(tmp_path: Path) -> None:
    tables = cell(base(), "deliveries", "dlv-0005", "shop_id", "shop-999")
    db = engine(tmp_path)
    report = import_book(db, book(tmp_path, tables), tenant_id=TENANT)
    assert [r.reason for r in rejects(report, entity="deliveries")] == [UNKNOWN_SHOP]
    assert "shop-999" in rejects(report, entity="deliveries")[0].detail


def test_a_file_that_mixes_two_shops_is_refused(tmp_path: Path) -> None:
    """A receivable for one shop and a delivery that went to another is the file
    contradicting itself; neither figure can be trusted, so it is refused."""
    tables = cell(base(), "receivables", "dlv-0001", "shop_id", "shop-002")
    db = engine(tmp_path)
    report = import_book(db, book(tmp_path, tables), tenant_id=TENANT)
    assert [r.reason for r in rejects(report, entity="receivables")] == [MIXES_TWO_SHOPS]
    assert "dlv-0001" not in [row["delivery_id"] for row in rows(db, "receivables")]


def test_a_receivable_that_disagrees_with_its_delivery_is_refused(tmp_path: Path) -> None:
    """What a delivery owes is what its lines add up to. A receivable that says
    otherwise is refused, not reconciled in its favour."""
    tables = cell(base(), "receivables", "dlv-0002", "total_naira", "50000.00")
    db = engine(tmp_path)
    report = import_book(db, book(tmp_path, tables), tenant_id=TENANT)
    assert [r.reason for r in rejects(report, entity="receivables")] == [TOTAL_MISMATCH]
    assert "5705000" in rejects(report, entity="receivables")[0].detail


def test_a_naira_figure_that_is_not_whole_kobo_is_refused(tmp_path: Path) -> None:
    """The conversion is visible: a third decimal place is a reject that names the
    kobo figure it would have become, not a rounded price."""
    tables = cell(base(), "skus", "SKU-003", "price_naira", "15750.005")
    db = engine(tmp_path)
    report = import_book(db, book(tmp_path, tables), tenant_id=TENANT)
    assert [r.reason for r in rejects(report, entity="skus")] == [BAD_MONEY]
    assert "1575000.5 kobo" in rejects(report, entity="skus")[0].detail
    assert "SKU-003" not in [row["sku"] for row in rows(db, "skus")]


def test_a_file_total_that_disagrees_with_its_lines_is_refused(tmp_path: Path) -> None:
    """The total is summed from the lines. A file that states a different one is
    telling two stories about the same money; it is refused, never preferred."""
    tables = cell(base(), "deliveries", "dlv-0002", "total_naira", "1.00")
    db = engine(tmp_path)
    report = import_book(db, book(tmp_path, tables), tenant_id=TENANT)
    assert [r.reason for r in rejects(report, entity="deliveries")] == [TOTAL_MISMATCH]
    assert "dlv-0002" not in [row["delivery_id"] for row in rows(db, "deliveries")]


def test_totals_are_computed_in_code(tmp_path: Path) -> None:
    """Nothing about a total is taken from the file: 2 x 18,500.00 + 5 x 4,200.50."""
    db = engine(tmp_path)
    import_book(db, book(tmp_path), tenant_id=TENANT)
    written = {row["delivery_id"]: row for row in rows(db, "deliveries")}
    assert written["dlv-0001"]["total_kobo"] == 2 * 1_850_000 + 5 * 420_050 == 5_800_250
    first = [row for row in rows(db, "delivery_lines") if row["delivery_id"] == "dlv-0001"]
    assert sorted(row["line_total_kobo"] for row in first) == [2_100_250, 3_700_000]
    receivable = {row["delivery_id"]: row for row in rows(db, "receivables")}
    assert receivable["dlv-0001"]["outstanding_kobo"] == 5_800_250 - 1_500_000


def test_a_duplicate_id_whose_content_changed_is_refused(tmp_path: Path) -> None:
    """A re-import repairs and adds; it never edits a row the book already holds.
    The changed delivery is refused and the book keeps its number."""
    db = engine(tmp_path)
    import_book(db, book(tmp_path), tenant_id=TENANT)
    before = {row["delivery_id"]: row for row in rows(db, "deliveries")}

    # The same delivery, timed an hour later: its content changed, its money did not.
    tables = cell(base(), "deliveries", "dlv-0002", "minute", "660")
    changed = tmp_path / "changed"
    report = import_book(db, book(changed, tables), tenant_id=TENANT)

    assert [r.reason for r in rejects(report, entity="deliveries")] == [DUP_IN_BOOK]
    assert "already in the book with different content" in \
        rejects(report, entity="deliveries")[0].detail
    after = {row["delivery_id"]: row for row in rows(db, "deliveries")}
    assert after["dlv-0002"]["total_kobo"] == before["dlv-0002"]["total_kobo"] == 5_705_000


def test_a_dry_run_previews_and_writes_nothing(tmp_path: Path) -> None:
    """The dry run's counts are the real import's counts, and nothing lands."""
    db = engine(tmp_path)
    dry = import_book(db, book(tmp_path), tenant_id=TENANT, dry_run=True)
    assert dry.counts["shops"] == {"new": 3, "unchanged": 0, "rejected": 0}
    assert dry.counts["deliveries"] == {"new": 6, "unchanged": 0, "rejected": 0}
    assert rows(db, "tenants") == [] and rows(db, "shops") == []
    assert rows(db, "connector_sync_runs") == []

    real = import_book(db, book(tmp_path), tenant_id=TENANT)
    assert real.counts == dry.counts and real.rejects == dry.rejects


def test_the_cli_previews_a_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = engine(tmp_path)
    folder = book(tmp_path)
    code = cli([str(folder), "--tenant-id", TENANT, "--dry-run",
                "--database-url", f"sqlite:///{tmp_path / 'book.db'}"])
    printed = capsys.readouterr().out
    assert code == 0
    assert "dry run" in printed and "6 added" in printed
    assert rows(db, "shops") == []


# --------------------------------------------------------------- the round trip


def test_the_second_import_changes_nothing(tmp_path: Path) -> None:
    """Idempotency by the distributor's own ids: the same file twice is one book,
    one set of proof lines, and no new book rows at all."""
    db = engine(tmp_path)
    first = import_book(db, book(tmp_path), tenant_id=TENANT)
    assert sum(c["new"] for c in first.counts.values()) == 31
    before = {table.name: rows(db, table.name) for table in BOOK_TABLES}
    proofs = len(rows(db, "proof_events"))

    second = import_book(db, book(tmp_path), tenant_id=TENANT)

    assert all(c["new"] == 0 and c["rejected"] == 0 for c in second.counts.values())
    assert second.counts["shops"] == {"new": 0, "unchanged": 3, "rejected": 0}
    assert second.counts["delivery_lines"] == {"new": 0, "unchanged": 9, "rejected": 0}
    assert {t.name: rows(db, t.name) for t in BOOK_TABLES} == before
    assert len(rows(db, "proof_events")) == proofs
    # The one thing a second read adds is its ledger line: proof that it happened.
    assert len(rows(db, "connector_sync_runs")) == 2


def test_a_later_book_moves_the_book_day(tmp_path: Path) -> None:
    """A nightly import of a newer export moves the book on, so overdue is counted
    from the day the book actually describes."""
    db = engine(tmp_path)
    import_book(db, book(tmp_path), tenant_id=TENANT)
    assert rows(db, "tenants")[0]["live_date"] == "2026-10-27"

    tables = base()
    tables["deliveries"].append(
        ["dlv-0007", "shop-002", "rep-01", "2026-11-03", "540", ""])
    tables["delivery_lines"].append(["dlv-0007", "SKU-004", "2", "9800.00", "", ""])
    report = import_book(db, book(tmp_path / "nov", tables), tenant_id=TENANT)

    assert report.live_date == "2026-11-03"
    assert rows(db, "tenants")[0]["live_date"] == "2026-11-03"
    # The invoice due on 30 Oct is late now; the one due 4 Nov is not yet.
    # 4,300,250 + 3,705,000 + 3,920,000 = 11,925,250 kobo.
    assert owed.totals(SqlGo(db).receivables(TENANT, None)).overdue_kobo == 11_925_250
    status = {row["delivery_id"]: row["status"] for row in rows(db, "receivables")}
    assert status == {"dlv-0001": "overdue", "dlv-0002": "overdue",
                      "dlv-0004": "overdue", "dlv-0005": "open"}


def test_imported_rows_carry_provenance(tmp_path: Path) -> None:
    """Every imported row names the import run that created it, and that run names
    the file, its digest and the moment it was read."""
    db = engine(tmp_path)
    report = import_book(db, book(tmp_path), tenant_id=TENANT)
    ledger = rows(db, "connector_sync_runs")
    assert len(ledger) == 1
    run = ledger[0]
    # 31 book rows plus the distributor row: what the source actually read.
    assert run["rows_mapped"] == 31 and run["rows_read"] == 32
    assert run["started_at"] == report.at
    assert run["connection_id"] == f"csv:book#{report.digest[:12]}"

    # One event per delivery and payment: the record types the proof policy
    # reasons about. Shops, reps, SKUs, lines and receivables have no type in it,
    # so their provenance is this run, not an event inventing a type.
    events = rows(db, "proof_events")
    assert len(events) == 6 + 3
    assert {e["witness_source"] for e in events} == {run["run_id"]}
    assert {e["at"] for e in events} == {report.at}
    assert {e["record_type"] for e in events} <= set(RECORD_TYPES)
    assert {e["record_type"] for e in events} == {
        "delivery", "payment_transfer", "payment_cash"}
    payments = sorted(e["record_id"] for e in events if e["record_type"] != "delivery")
    assert payments == ["pay-0001", "pay-0002", "pay-0003"]

    # A delivery and a payment also carry the book's own proof columns: the
    # distributor's record is one party's word, so the level is estimated.
    delivery = {row["delivery_id"]: row for row in rows(db, "deliveries")}["dlv-0001"]
    assert delivery["proof_level"] == "estimated"
    assert "distributor_record" in delivery["witnesses"]
    payment = {row["payment_id"]: row for row in rows(db, "payments")}["pay-0001"]
    assert payment["proof_level"] == "estimated"
    assert payment["status"] == "reported"


# ------------------------------------------------------------------- acceptance


def test_overdue_matches_a_hand_count(tmp_path: Path) -> None:
    """The acceptance check: the overdue figure Mimi would show equals the sum a
    person gets by reading the same four receivables.

    By hand, from tests/fixtures/books/small (naira): the book's day is its newest
    delivery, 27 Oct 2026. Then

        dlv-0001  due 09 Oct, 58,002.50 - 15,000.00 paid = 43,002.50   overdue
        dlv-0002  due 13 Oct, 57,050.00 - 20,000.00 paid = 37,050.00   overdue
        dlv-0004  due 30 Oct                                            not yet due
        dlv-0005  due 04 Nov                                            not yet due

        43,002.50 + 37,050.00 = 80,052.50 naira = 8,005,250 kobo
    """
    book_day = "2026-10-27"
    hand = [(5_800_250, 1_500_000, "2026-10-09"),
            (5_705_000, 2_000_000, "2026-10-13"),
            (3_920_000, 0, "2026-10-30"),
            (5_000_000, 0, "2026-11-04")]
    expected = sum(total - paid for total, paid, due in hand if due < book_day)
    assert expected == 8_005_250

    db = engine(tmp_path)
    import_book(db, book(tmp_path), tenant_id=TENANT)
    assert rows(db, "tenants")[0]["live_date"] == book_day

    counted = owed.totals(SqlGo(db).receivables(TENANT, None))
    assert counted.overdue_kobo == expected
    assert counted.owed_kobo == expected + 3_920_000 + 5_000_000
    assert [s.shop_id for s in counted.debtors()] == ["shop-001", "shop-002"]


def test_a_real_tenant_reads_as_its_own_book(tmp_path: Path) -> None:
    """A tenant made from a real book is not the demo world: its rows say so, and
    the console's reads answer from its own numbers."""
    db = engine(tmp_path)
    import_book(db, book(tmp_path), tenant_id=TENANT)
    tenant = rows(db, "tenants")[0]
    assert tenant["simulated"] is False and tenant["name"] == "Sunline Distributors"
    assert {row["simulated"] for row in rows(db, "shops")} == {0}

    listed = reads.list_shops(db, TENANT)
    assert listed["total"] == 3
    assert listed["overdue_total_kobo"] == 8_005_250
    assert sorted(shop["shop_id"] for shop in listed["shops"]) == [
        "shop-001", "shop-002", "shop-003"]
    shop = reads.get_shop(db, TENANT, "shop-001")
    # shop-001 owes on two deliveries (dlv-0001 and dlv-0004); only one is late.
    assert shop["shop"]["outstanding_kobo"] == 4_300_250 + 3_920_000
    assert shop["shop"]["overdue_kobo"] == 4_300_250
    assert [row["delivery_id"] for row in shop["deliveries"]] == ["dlv-0004", "dlv-0001"]


def test_an_empty_book_shows_nothing_not_a_sample(tmp_path: Path) -> None:
    """A book whose shops are all refused still exists as a tenant, and reads as
    empty. It never falls back to the demo world's shops."""
    empty_shops = {**base(), "shops": [base()["shops"][0]]}
    db = engine(tmp_path)
    report = import_book(db, book(tmp_path, empty_shops), tenant_id=TENANT)

    assert report.counts["shops"] == {"new": 0, "unchanged": 0, "rejected": 0}
    assert len(report.rejects) > 0
    assert rows(db, "tenants")[0]["tenant_id"] == TENANT
    assert rows(db, "shops") == [] and rows(db, "deliveries") == []
    listed = reads.list_shops(db, TENANT)
    assert listed == {**listed, "total": 0, "owed_total_kobo": 0, "overdue_total_kobo": 0}


def test_a_demo_tenant_is_never_imported_into(tmp_path: Path) -> None:
    """The synthetic world is synthetic. A real book goes into a tenant of its own,
    so nobody can mistake Coral Lane's figures for a distributor's."""
    db = engine(tmp_path)
    world = load_world()
    seed_world(db, world)
    tables = cell(base(), "distributor", TENANT, "tenant_id", world.tenant_id)
    with pytest.raises(ConnectorError, match="synthetic demo world"):
        import_book(db, book(tmp_path, tables), tenant_id=world.tenant_id)



def test_a_record_type_the_policy_does_not_have_is_never_written(tmp_path: Path) -> None:
    """Provenance is written through the proof policy, not around it.

    Two payments on one delivery is the case that catches both halves: a generic
    "payment" type throws away cash-versus-transfer, and keying the event by the
    delivery would collide on the second one. Every event carries a type the
    policy knows and the id of its own record.
    """
    tables = base()
    tables["payments"].append(
        ["pay-0004", "shop-001", "dlv-0001", "2026-10-22", "500.00", "cash", ""])
    db = engine(tmp_path)
    import_book(db, book(tmp_path, tables), tenant_id=TENANT)

    events = rows(db, "proof_events")
    assert len({event["event_id"] for event in events}) == len(events)
    assert {event["record_type"] for event in events} <= set(RECORD_TYPES)
    money = [event for event in events if event["record_type"] != "delivery"]
    assert sorted(event["record_type"] for event in money) == [
        "payment_cash", "payment_cash", "payment_transfer", "payment_transfer"]
    assert sorted(event["record_id"] for event in money) == [
        "pay-0001", "pay-0002", "pay-0003", "pay-0004"]


def test_an_as_of_before_the_book_day_is_refused(tmp_path: Path) -> None:
    """Half applying a stale day is worse than refusing it: the stored word and
    the overdue sum would be counted from two different days."""
    db = engine(tmp_path)
    import_book(db, book(tmp_path), tenant_id=TENANT)  # the book's day is 27 Oct
    with pytest.raises(ConnectorError, match="already 2026-10-27"):
        import_book(db, book(tmp_path), tenant_id=TENANT, as_of="2026-10-20")
    assert rows(db, "tenants")[0]["live_date"] == "2026-10-27"


def test_the_ledger_counts_the_rows_the_read_saw(tmp_path: Path) -> None:
    """``rows_read`` means read, so a re-import that matches everything says so."""
    db = engine(tmp_path)
    import_book(db, book(tmp_path), tenant_id=TENANT)
    import_book(db, book(tmp_path), tenant_id=TENANT)
    ledger = sorted(rows(db, "connector_sync_runs"), key=lambda run: run["started_at"])
    assert (ledger[0]["rows_read"], ledger[0]["rows_mapped"]) == (32, 31)
    assert (ledger[1]["rows_read"], ledger[1]["rows_mapped"]) == (32, 0)


def test_phones_are_stored_in_one_canonical_form(tmp_path: Path) -> None:
    """A shop is reachable only if its stored number matches the E.164 the
    channel sends, so the import normalises it once and says what it changed."""
    tables = base()
    cell(tables, "shops", "shop-001", "phone", "0803 123 4567")
    cell(tables, "shops", "shop-002", "phone", "08031234568")
    cell(tables, "shops", "shop-003", "phone", "+234 803 123 4569")
    cell(tables, "reps", "rep-01", "phone", "0803 111 2222")
    cell(tables, "distributor", TENANT, "owner_phones", "0801 000 0001")
    db = engine(tmp_path)
    report = import_book(db, book(tmp_path, tables), tenant_id=TENANT)

    assert {shop["phone"] for shop in rows(db, "shops")} == {
        "+2348031234567", "+2348031234568", "+2348031234569"}
    assert {rep["phone"] for rep in rows(db, "reps")} == {
        "+2348031112222", "+2340000000202"}
    assert rows(db, "tenants")[0]["owner_phones"] == '["+2348010000001"]'

    # What the channel will actually ask about, and the routing it has to get.
    directory = SqlGo(db).directory(TENANT)
    assert directory.shop_for("+2348031234567") == "shop-001"
    assert directory.shop_for("+2348031234568") == "shop-002"
    assert "+2348010000001" in directory.owner_phones
    # The number as typed is still named, so nobody wonders what happened to it.
    assert "0803 123 4567" in " ".join(report.notes)
    assert "+2348031234567" in " ".join(report.notes)


def test_a_re_import_does_not_re_report_the_same_phone(tmp_path: Path) -> None:
    """The store now holds the canonical form, so the same file changes nothing
    and says nothing about phones the second time."""
    tables = cell(base(), "shops", "shop-001", "phone", "0803 123 4567")
    db = engine(tmp_path)
    first = import_book(db, book(tmp_path, tables), tenant_id=TENANT)
    again = import_book(db, book(tmp_path, tables), tenant_id=TENANT)
    assert "0803 123 4567" in " ".join(first.notes)
    assert again.notes == () and again.counts["shops"]["new"] == 0


def test_a_phone_too_short_to_be_a_line_is_refused(tmp_path: Path) -> None:
    """A number nobody could dial is a reject that names it, never a key that
    silently matches nobody."""
    tables = cell(base(), "shops", "shop-001", "phone", "12345")
    db = engine(tmp_path)
    report = import_book(db, book(tmp_path, tables), tenant_id=TENANT)
    assert [r.reason for r in rejects(report, entity="shops")] == [BAD_PHONE]
    assert "12345" in rejects(report, entity="shops")[0].detail
    assert "shop-001" not in [shop["shop_id"] for shop in rows(db, "shops")]


def test_a_negative_amount_paid_is_refused(tmp_path: Path) -> None:
    """Minus one hundred naira paid would store a debt bigger than the invoice."""
    tables = cell(base(), "receivables", "dlv-0001", "paid_naira", "-100")
    db = engine(tmp_path)
    report = import_book(db, book(tmp_path, tables), tenant_id=TENANT)
    assert [r.reason for r in rejects(report, entity="receivables")] == [BAD_MONEY]
    assert "never negative" in rejects(report, entity="receivables")[0].detail
    assert "dlv-0001" not in [row["delivery_id"] for row in rows(db, "receivables")]


def test_two_lines_with_the_same_number_refuse_the_delivery(tmp_path: Path) -> None:
    """The line number is part of the delivery's key: a repeat is a refused
    delivery, not a primary-key crash out of the write."""
    tables = base()
    tables["delivery_lines"][0] = ["line_no", *tables["delivery_lines"][0]]
    for row in tables["delivery_lines"][1:]:
        row.insert(0, "1" if row[0] == "dlv-0001" else "")
    db = engine(tmp_path)
    report = import_book(db, book(tmp_path, tables), tenant_id=TENANT)
    assert [r.reason for r in rejects(report, entity="deliveries")] == [DUP_IN_FILE]
    assert "two lines numbered 1" in rejects(report, entity="deliveries")[0].detail
    assert "dlv-0001" not in [row["delivery_id"] for row in rows(db, "deliveries")]


def test_an_id_twice_in_one_file_is_named_not_a_crash(tmp_path: Path) -> None:
    """Every keyed table dedupes in the source, so a repeated id is a named row
    rather than an IntegrityError after half a plan."""
    tables = base()
    tables["receivables"].append(list(tables["receivables"][1]))
    tables["payments"].append(list(tables["payments"][1]))
    db = engine(tmp_path)
    report = import_book(db, book(tmp_path, tables), tenant_id=TENANT)

    assert [r.reason for r in rejects(report, entity="receivables")] == [DUP_IN_FILE]
    assert [r.reason for r in rejects(report, entity="payments")] == [DUP_IN_FILE]
    assert len(rows(db, "receivables")) == 4 and len(rows(db, "payments")) == 3
    assert len(rows(db, "shops")) == 3

    # A copy that disagrees is refused outright: neither version is the book.
    changed = base()
    copy = list(changed["payments"][1])
    copy[4] = "1.00"
    changed["payments"].append(copy)
    (tmp_path / "changed").mkdir()
    other = engine(tmp_path / "changed")
    report = import_book(other, book(tmp_path / "changed", changed), tenant_id=TENANT)
    assert [r.reason for r in rejects(report, entity="payments")] == [
        DUP_IN_FILE, DUP_IN_FILE]
    assert [row["payment_id"] for row in rows(other, "payments")] == [
        "pay-0002", "pay-0003"]


def test_a_book_naming_another_tenant_is_refused_whatever_else_it_is_missing(
        tmp_path: Path) -> None:
    """Which book this is comes first: a half-filled row cannot carry another
    distributor's data into an existing tenant."""
    db = engine(tmp_path)
    import_book(db, book(tmp_path), tenant_id=TENANT)
    tables = cell(base(), "distributor", TENANT, "tenant_id", "someone-else")
    cell(tables, "distributor", "someone-else", "name", "")
    with pytest.raises(ConnectorError, match="belongs to 'someone-else'"):
        import_book(db, book(tmp_path / "other", tables), tenant_id=TENANT)
    assert len(rows(db, "shops")) == 3 and len(rows(db, "deliveries")) == 6


def test_two_imports_racing_never_move_the_book_day_back(tmp_path: Path) -> None:
    """A stale plan that lands second must not overwrite a newer book day, and
    the overdue words must follow the day actually in effect."""
    db = engine(tmp_path)
    import_book(db, book(tmp_path), tenant_id=TENANT)  # the book's day is 27 Oct

    older = base()
    older["deliveries"].append(["dlv-0007", "shop-001", "rep-01", "2026-11-03", "540", ""])
    older["delivery_lines"].append(["dlv-0007", "SKU-004", "1", "9800.00", "", ""])
    older["receivables"].append(["dlv-0007", "shop-001", "2026-11-05", "9800.00", "0.00"])
    newer = base()
    newer["deliveries"].append(["dlv-0008", "shop-002", "rep-01", "2026-11-10", "540", ""])
    newer["delivery_lines"].append(["dlv-0008", "SKU-004", "1", "9800.00", "", ""])

    at = "2026-11-19T09:00:00+00:00"
    plan_older = build_plan(db, read_book(book(tmp_path / "older", older), tenant_id=TENANT,
                                          read_at=at), tenant_id=TENANT, at=at)
    plan_newer = build_plan(db, read_book(book(tmp_path / "newer", newer), tenant_id=TENANT,
                                          read_at=at), tenant_id=TENANT, at=at)
    write_plan(db, plan_newer)
    write_plan(db, plan_older)

    assert rows(db, "tenants")[0]["live_date"] == "2026-11-10"
    status = {row["delivery_id"]: row["status"] for row in rows(db, "receivables")}
    assert status["dlv-0007"] == "overdue"  # due 5 Nov, book day 10 Nov
    assert status["dlv-0004"] == "overdue"  # due 30 Oct


def test_the_cli_refuses_sqlite_in_production(tmp_path: Path,
                                              capsys: pytest.CaptureFixture[str],
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """A production import into a file the production API never reads is a book
    nobody sees."""
    monkeypatch.setenv("MIMI_ENV", "production")
    code = cli([str(book(tmp_path)), "--tenant-id", TENANT,
                "--database-url", f"sqlite:///{tmp_path / 'prod.db'}"])
    assert code == 1
    assert "production needs a Postgres database" in capsys.readouterr().err


def test_the_import_path_is_code_only() -> None:
    """No model call anywhere on the import path: an import is arithmetic and
    rules, and a model in it would make a figure unreproducible."""
    package = Path(__file__).parents[1] / "mimi" / "connectors" / "book"
    source = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
    for forbidden in ("mimi.models", "mimi.harness", "FakeModel", "MetaModel"):
        assert forbidden not in source, forbidden


def _names(rows_: Iterable[dict[str, Any]], key: str) -> list[str]:
    return [str(row[key]) for row in rows_]
