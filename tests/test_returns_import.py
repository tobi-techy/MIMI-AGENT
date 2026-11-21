"""Returns imported from a distributor's own book (MIAG-10).

The fixture book in ``tests/fixtures/books/small`` carries two returns: one
still with the rep and one already credited. These tests prove the rules the
importer keeps for them: the value summed from the lines, the status checked
against the book's closed list, the reason checked against the contract's,
provenance through the proof policy, every reference checked against the book,
a re-import unchanged and a changed row refused rather than overwritten. The
last tests are the aged read the brief stands on.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.connectors.base import ConnectorError
from mimi.connectors.book import import_book
from mimi.connectors.book.rows import (
    BAD_REASON,
    BAD_STATUS,
    DUP_IN_BOOK,
    MIXES_TWO_SHOPS,
    NO_LINES,
    TOTAL_MISMATCH,
    UNKNOWN_DELIVERY,
    UNKNOWN_REP,
    UNKNOWN_RETURN,
    UNKNOWN_SHOP,
    UNKNOWN_SKU,
)
from mimi.provenance import POLICY_VERSION
from mimi.store import ensure_schema, make_engine
from mimi.store import schema as s
from mimi.store.returns import aged_returns
from mimi.store.returns import main as returns_cli
from mimi.types import RETURN_BOOK_STATUSES
from tests.test_book_import import FIXTURES, cell, write

TENANT = "sunline-dist"
ALL_TABLES = ("distributor", "reps", "shops", "skus", "deliveries", "delivery_lines",
              "receivables", "payments", "returns", "return_lines")


def book_tables() -> dict[str, list[list[str]]]:
    """The fixture book with its two return tables, editable before writing it out."""
    out: dict[str, list[list[str]]] = {}
    for table in ALL_TABLES:
        with (FIXTURES / "csv" / f"{table}.csv").open(newline="", encoding="utf-8") as fh:
            out[table] = list(csv.reader(fh))
    return out


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[sa.Engine]:
    made = make_engine(f"sqlite:///{tmp_path / 'returns.db'}")
    ensure_schema(made)
    yield made
    made.dispose()


def _import(db: sa.Engine, folder: Path, **kwargs: Any) -> Any:
    return import_book(db, folder, tenant_id=TENANT, **kwargs)


def _rows(db: sa.Engine, table: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sa.select(s.__dict__[table])).mappings()]


def _rejects(report: Any, reason: str) -> list[Any]:
    return [r for r in report.rejects if r.reason == reason]


# ------------------------------------------------------------------- the book


def test_a_return_lands_with_its_value_summed_from_the_lines(db: sa.Engine,
                                                             tmp_path: Path) -> None:
    """The value is quantity times price, summed in code, never the file's total;
    the status and the reason are the book's own closed lists."""
    report = _import(db, write(book_tables(), tmp_path / "book"))
    assert report.rejects == ()
    pending = {r["return_id"]: r for r in _rows(db, "returns") if r["return_id"] == "ret-0001"}
    one = pending["ret-0001"]
    assert one["shop_id"] == "shop-002" and one["rep_id"] == "rep-01"
    assert one["status"] == "pending" and one["reason"] == "damaged"
    assert one["delivery_id"] == "dlv-0002" and one["source"] == "imported"
    assert one["raised_on"] == "2026-10-24"
    assert one["raised_day"] == date.fromisoformat("2026-10-24").toordinal()
    assert one["value_drafted_kobo"] == 1_575_000
    # Nothing has come back yet, so the returned value is zero.
    assert one["value_returned_kobo"] == 0
    credited = next(r for r in _rows(db, "returns") if r["return_id"] == "ret-0002")
    assert credited["status"] == "credited" and credited["value_returned_kobo"] == 1_850_000
    line = _rows(db, "return_lines")[0]
    assert line["sku"] == "SKU-003" and line["item"] == "Peak Milk 400g"
    assert line["quantity_drafted"] == 1 and line["value_drafted_kobo"] == 1_575_000


def test_a_return_carries_provenance_through_the_policy(db: sa.Engine,
                                                        tmp_path: Path) -> None:
    """A return is the distributor's own record: estimated, one witness, and one
    append-only event naming the import run that read it."""
    folder = write(book_tables(), tmp_path / "book")
    _import(db, folder)
    run = _rows(db, "connector_sync_runs")[0]
    for one in _rows(db, "returns"):
        assert one["proof_level"] == "estimated"
        assert one["proof_policy_version"] == POLICY_VERSION
    events = [e for e in _rows(db, "proof_events") if e["record_type"] == "return"]
    assert {e["record_id"] for e in events} == {"ret-0001", "ret-0002"}
    assert {e["witness_source"] for e in events} == {run["run_id"]}
    assert {e["new_level"] for e in events} == {"estimated"}


def test_the_ledger_counts_the_return_rows_it_read(db: sa.Engine, tmp_path: Path) -> None:
    """``rows_read`` counts the return tables too, and a re-import of the same
    book maps nothing."""
    folder = write(book_tables(), tmp_path / "book")
    _import(db, folder)
    _import(db, folder)
    runs = sorted(_rows(db, "connector_sync_runs"), key=lambda r: r["started_at"])
    assert runs[0]["rows_read"] == 36 and runs[0]["rows_mapped"] == 35
    assert runs[1]["rows_read"] == 36 and runs[1]["rows_mapped"] == 0


def test_a_book_with_no_returns_is_not_refused(db: sa.Engine, tmp_path: Path) -> None:
    """An export taken before returns were recorded is a gap, not a bad row."""
    tables = book_tables()
    del tables["returns"], tables["return_lines"]
    report = _import(db, write(tables, tmp_path / "book"))
    assert report.rejects == ()
    assert _rows(db, "returns") == []


def test_the_two_return_tables_come_together(db: sa.Engine, tmp_path: Path) -> None:
    tables = book_tables()
    del tables["return_lines"]
    with pytest.raises(ConnectorError, match="come together"):
        _import(db, write(tables, tmp_path / "book"))


# ------------------------------------------------------------------ refusals


def _refused(tmp_path: Path, db: sa.Engine, reason: str,
             tables: dict[str, list[list[str]]]) -> list[Any]:
    report = _import(db, write(tables, tmp_path / "book"))
    found = _rejects(report, reason)
    assert found, [f"{r.where}: {r.detail} [{r.reason}]" for r in report.rejects]
    return report


def test_a_return_for_an_unknown_shop_is_refused(db: sa.Engine, tmp_path: Path) -> None:
    tables = cell(book_tables(), "returns", "ret-0001", "shop_id", "shop-999")
    report = _refused(tmp_path, db, UNKNOWN_SHOP, tables)
    assert [r.external_id for r in _rejects(report, UNKNOWN_SHOP)] == ["ret-0001"]
    assert [r["return_id"] for r in _rows(db, "returns")] == ["ret-0002"]


def test_a_return_naming_an_unknown_delivery_is_refused(db: sa.Engine,
                                                        tmp_path: Path) -> None:
    tables = cell(book_tables(), "returns", "ret-0001", "delivery_id", "dlv-9999")
    _refused(tmp_path, db, UNKNOWN_DELIVERY, tables)


def test_a_return_off_another_shops_delivery_is_refused(db: sa.Engine,
                                                        tmp_path: Path) -> None:
    tables = cell(book_tables(), "returns", "ret-0001", "delivery_id", "dlv-0005")
    _refused(tmp_path, db, MIXES_TWO_SHOPS, tables)


def test_a_return_for_an_unknown_sku_is_refused(db: sa.Engine, tmp_path: Path) -> None:
    tables = cell(book_tables(), "return_lines", "ret-0001", "sku", "SKU-999")
    _refused(tmp_path, db, UNKNOWN_SKU, tables)


def test_a_return_carried_by_an_unknown_rep_is_refused(db: sa.Engine,
                                                       tmp_path: Path) -> None:
    tables = cell(book_tables(), "returns", "ret-0001", "rep_id", "rep-99")
    _refused(tmp_path, db, UNKNOWN_REP, tables)


def test_a_status_the_book_does_not_know_is_refused(db: sa.Engine, tmp_path: Path) -> None:
    tables = cell(book_tables(), "returns", "ret-0001", "status", "in_the_van")
    _refused(tmp_path, db, BAD_STATUS, tables)


def test_every_book_status_is_accepted(tmp_path: Path) -> None:
    """The closed list is the whole vocabulary: each word reads, however it is
    spelled in the file."""
    for status in RETURN_BOOK_STATUSES:
        made = make_engine(f"sqlite:///{tmp_path / f'{status}.db'}")
        ensure_schema(made)
        tables = cell(book_tables(), "returns", "ret-0001", "status",
                      status.upper().replace("_", " "))
        assert _import(made, write(tables, tmp_path / f"book-{status}")).rejects == ()
        made.dispose()


def test_a_reason_outside_the_closed_list_is_refused(db: sa.Engine,
                                                     tmp_path: Path) -> None:
    tables = cell(book_tables(), "returns", "ret-0001", "reason", "not selling")
    _refused(tmp_path, db, BAD_REASON, tables)


def test_a_return_with_no_lines_is_refused(db: sa.Engine, tmp_path: Path) -> None:
    tables = book_tables()
    tables["return_lines"] = [tables["return_lines"][0]] + [
        r for r in tables["return_lines"][1:] if r[0] != "ret-0001"]
    _refused(tmp_path, db, NO_LINES, tables)


def test_a_stated_total_that_disagrees_is_refused(db: sa.Engine, tmp_path: Path) -> None:
    tables = cell(book_tables(), "returns", "ret-0001", "total_naira", "9000.00")
    _refused(tmp_path, db, TOTAL_MISMATCH, tables)


def test_a_line_for_a_return_not_listed_is_refused(db: sa.Engine, tmp_path: Path) -> None:
    tables = book_tables()
    tables["return_lines"] = tables["return_lines"] + [
        ["ret-0009", "1", "SKU-001", "1", "18500.00", ""]]
    _refused(tmp_path, db, UNKNOWN_RETURN, tables)


def test_a_changed_return_is_refused_never_overwritten(db: sa.Engine,
                                                       tmp_path: Path) -> None:
    """An import adds rows and never edits a book: a later export that moves a
    return on is a correction, and the correction path does not exist yet."""
    folder = write(book_tables(), tmp_path / "book")
    _import(db, folder)
    moved = cell(book_tables(), "returns", "ret-0001", "status", "in_transit")
    report = _import(db, write(moved, tmp_path / "book2"))
    assert [r.reason for r in _rejects(report, DUP_IN_BOOK)] == [DUP_IN_BOOK]
    assert next(r["status"] for r in _rows(db, "returns")
                if r["return_id"] == "ret-0001") == "pending"


def test_a_dry_run_writes_no_return(db: sa.Engine, tmp_path: Path) -> None:
    report = _import(db, write(book_tables(), tmp_path / "book"), dry_run=True)
    assert report.rejects == () and report.counts["returns"]["new"] == 2
    assert _rows(db, "returns") == []


# ---------------------------------------------------------------- the aged read


def test_the_aged_read_lists_what_is_still_out(db: sa.Engine, tmp_path: Path) -> None:
    """Only an open return ages: the pending one is three days out on the book's
    own day, and the credited one is closed."""
    _import(db, write(book_tables(), tmp_path / "book"))
    aged = aged_returns(db, TENANT)
    assert [r.return_id for r in aged] == ["ret-0001"]
    one = aged[0]
    assert (one.shop_name, one.rep_name) == ("Blessed Kiosk", "Abiola Nwosu")
    assert one.days_open == 3 and one.value_kobo == 1_575_000 and one.status == "pending"


def test_the_aged_read_says_when_nothing_is_open(db: sa.Engine, tmp_path: Path) -> None:
    _import(db, write(book_tables(), tmp_path / "book"))
    with db.begin() as conn:
        conn.execute(s.returns.update().values(status="credited"))
    assert aged_returns(db, TENANT) == []


def test_the_cli_prints_the_rows_and_their_json(db: sa.Engine, tmp_path: Path,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    url = str(db.url)
    _import(db, write(book_tables(), tmp_path / "book"))
    assert returns_cli(["--tenant-id", TENANT, "--db", url]) == 0
    assert "Blessed Kiosk" in capsys.readouterr().out
    assert returns_cli(["--tenant-id", TENANT, "--db", url, "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed[0]["return_id"] == "ret-0001" and printed[0]["days_open"] == 3


def test_the_cli_names_a_tenant_it_cannot_find(tmp_path: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    db = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    ensure_schema(db)
    assert returns_cli(["--tenant-id", "nobody", "--db", str(db.url)]) == 2
    assert "no tenant" in capsys.readouterr().err
