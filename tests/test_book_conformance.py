"""Conformance: every book source must produce the same canonical rows.

One small synthetic book (``tests/fixtures/books/small``: 3 shops, 2 reps, 4
SKUs, 6 deliveries with 9 lines, 4 receivables, 3 payments) exists in two shapes:
a folder of CSV files and one workbook with a sheet per table. Both are read by
the CSV/XLSX source through different code paths -- text and Excel date cells,
strings and numbers -- so identical canonical rows across the two proves the
source reads the *book*, not one file format.

The suite is shared on purpose. A new source (Google Sheets, Zoho Books) is added
by putting one more entry in ``SOURCES`` below; the counts, the rejects and the
canonical rows are then checked for it without writing a second test. A source
that cannot pass is not nearly done: it is not shippable
(``plans/mimi-mvp/data-architecture.md``).

Only what a source is allowed to decide for itself is excluded from the
comparison: ``source``, ``label``, ``digest`` and each row's ``where`` (which file
or row it was read from). Everything that reaches the book must match exactly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from mimi.connectors.book.csv_source import read_book
from mimi.connectors.book.rows import Reading
from mimi.connectors.book.writer import import_book
from mimi.store.db import ensure_schema, make_engine

FIXTURES = Path(__file__).parent / "fixtures" / "books" / "small"
TENANT = "sunline-dist"
READ_AT = "2026-11-18T09:00:00+00:00"

#: What the book holds, in canonical rows. A source that reports anything else
#: has dropped or invented rows.
EXPECTED = {"reps": 2, "shops": 3, "skus": 4, "deliveries": 6, "receivables": 4,
            "payments": 3, "delivery_lines": 9}


def _read(path: Path) -> Reading:
    return read_book(path, tenant_id=TENANT, read_at=READ_AT)


#: Every source the suite checks, by name. Add one line per new source.
SOURCES: dict[str, Callable[[], Reading]] = {
    "csv folder": lambda: _read(FIXTURES / "csv"),
    "xlsx workbook": lambda: _read(FIXTURES / "book.xlsx"),
}


def canonical(reading: Reading) -> dict[str, Any]:
    """The part of a reading two sources must agree on, exactly."""

    def rows(items: tuple[Any, ...]) -> list[dict[str, Any]]:
        return [{k: v for k, v in asdict(row).items() if k != "provenance"}
                for row in items]

    deliveries = []
    for delivery in reading.deliveries:
        one = {k: v for k, v in asdict(delivery).items() if k not in ("provenance", "lines")}
        one["lines"] = rows(delivery.lines)
        deliveries.append(one)
    return {
        "distributor": None if reading.distributor is None else
        {k: v for k, v in asdict(reading.distributor).items() if k != "provenance"},
        "reps": rows(reading.reps), "shops": rows(reading.shops), "skus": rows(reading.skus),
        "deliveries": deliveries, "receivables": rows(reading.receivables),
        "payments": rows(reading.payments),
    }


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_a_source_reads_the_whole_book_with_no_rejects(name: str) -> None:
    """The fixture book is clean: nothing in it may be refused."""
    reading = SOURCES[name]()
    assert reading.rejects == ()
    counts = canonical(reading)
    assert {key: len(value) for key, value in counts.items() if isinstance(value, list)} \
        == {key: EXPECTED[key] for key in counts if isinstance(counts[key], list)}
    assert sum(len(row["lines"]) for row in counts["deliveries"]) == \
        EXPECTED["delivery_lines"]


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_a_source_produces_the_same_canonical_rows(name: str) -> None:
    """Every source agrees with the CSV folder, value for value."""
    assert canonical(SOURCES[name]()) == canonical(SOURCES["csv folder"]())


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_money_and_dates_arrive_typed(name: str) -> None:
    """Kobo is an int, a day is a date, in every source: the boundary rules hold
    before a row reaches the writer."""
    reading = SOURCES[name]()
    assert [sku.price_kobo for sku in reading.skus] == [1850000, 420050, 1575000, 980000]
    assert reading.deliveries[0].total_kobo == 5800250
    assert reading.deliveries[0].delivered_on.isoformat() == "2026-10-02"
    assert reading.deliveries[0].lines[0].expires_on is not None
    assert reading.payments[2].method == "cash_to_rep"


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_a_source_writes_the_same_book(name: str, tmp_path: Path) -> None:
    """Both shapes land the same rows in the store, and the second import of
    either changes nothing."""
    engine = make_engine(f"sqlite:///{tmp_path / 'book.db'}")
    ensure_schema(engine)
    first = import_book(engine, _path(name), tenant_id=TENANT)
    assert sum(c["new"] for c in first.counts.values()) == sum(EXPECTED.values())
    again = import_book(engine, _path(name), tenant_id=TENANT)
    assert all(c["new"] == 0 and c["rejected"] == 0 for c in again.counts.values())


def _path(name: str) -> Path:
    return FIXTURES / ("csv" if name == "csv folder" else "book.xlsx")


def test_the_two_shapes_are_not_the_same_file() -> None:
    """A guard on the suite itself: if the shapes became one file (a symlink, a
    copy), the comparison above would pass without testing anything."""
    csv_reading = SOURCES["csv folder"]()
    xlsx_reading = SOURCES["xlsx workbook"]()
    assert csv_reading.source == "csv" and xlsx_reading.source == "xlsx"
    assert csv_reading.digest != xlsx_reading.digest


def test_a_source_may_differ_only_in_provenance() -> None:
    """What a source is allowed to decide: where a row came from, nothing else."""
    csv_reading = SOURCES["csv folder"]()
    shifted = replace(csv_reading, label="elsewhere")
    assert canonical(shifted) == canonical(csv_reading)
