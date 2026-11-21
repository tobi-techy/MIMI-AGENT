"""Conformance: every book source must produce the same canonical rows.

One small synthetic book (``tests/fixtures/books/small``: 3 shops, 2 reps, 4
SKUs, 6 deliveries with 9 lines, 4 receivables, 3 payments, 2 returns with 2
lines) exists in two shapes:
a folder of CSV files and one workbook with a sheet per table. Both are read by
the CSV/XLSX source through different code paths -- text and Excel date cells,
strings and numbers -- so identical canonical rows across the two proves the
source reads the *book*, not one file format.

The suite is shared on purpose. A new source (Google Sheets, Zoho Books) is added
by putting one more ``Source`` in ``SOURCES`` below, with the fixture it reads;
the counts, the rejects, the canonical rows and the book it writes are then
checked for it without writing a second test. Each entry carries its own fixture,
so a new source is never quietly checked against the workbook in place of its own
file. A source that cannot pass is not nearly done: it is not shippable
(``plans/mimi-mvp/data-architecture.md``).

Only what a source is allowed to decide for itself is excluded from the
comparison: ``source``, ``label``, ``digest`` and each row's ``where`` (which file
or row it was read from). Everything that reaches the book must match exactly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
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
            "payments": 3, "delivery_lines": 9, "returns": 2, "return_lines": 2}


def _read(path: Path) -> Reading:
    return read_book(path, tenant_id=TENANT, read_at=READ_AT)


@dataclass(frozen=True)
class Source:
    """One source under test: the fixture it reads, and how it is read."""

    name: str
    path: Path

    def read(self) -> Reading:
        return _read(self.path)


#: Every source the suite checks. Add one entry per new source, with its own file.
SOURCES: dict[str, Source] = {
    source.name: source for source in (
        Source("csv folder", FIXTURES / "csv"),
        Source("xlsx workbook", FIXTURES / "book.xlsx"),
    )
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
    returns = []
    for one_return in reading.returns:
        one = {k: v for k, v in asdict(one_return).items() if k not in ("provenance", "lines")}
        one["lines"] = rows(one_return.lines)
        returns.append(one)
    return {
        "distributor": None if reading.distributor is None else
        {k: v for k, v in asdict(reading.distributor).items() if k != "provenance"},
        "reps": rows(reading.reps), "shops": rows(reading.shops), "skus": rows(reading.skus),
        "deliveries": deliveries, "receivables": rows(reading.receivables),
        "payments": rows(reading.payments), "returns": returns,
    }


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_a_source_reads_the_whole_book_with_no_rejects(name: str) -> None:
    """The fixture book is clean: nothing in it may be refused."""
    reading = SOURCES[name].read()
    assert reading.rejects == ()
    counts = canonical(reading)
    assert {key: len(value) for key, value in counts.items() if isinstance(value, list)} \
        == {key: EXPECTED[key] for key in counts if isinstance(counts[key], list)}
    assert sum(len(row["lines"]) for row in counts["deliveries"]) == \
        EXPECTED["delivery_lines"]
    assert sum(len(row["lines"]) for row in counts["returns"]) == \
        EXPECTED["return_lines"]


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_a_source_produces_the_same_canonical_rows(name: str) -> None:
    """Every source agrees with the CSV folder, value for value."""
    assert canonical(SOURCES[name].read()) == canonical(SOURCES["csv folder"].read())


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_money_and_dates_arrive_typed(name: str) -> None:
    """Kobo is an int, a day is a date, in every source: the boundary rules hold
    before a row reaches the writer."""
    reading = SOURCES[name].read()
    assert [sku.price_kobo for sku in reading.skus] == [1850000, 420050, 1575000, 980000]
    assert reading.deliveries[0].total_kobo == 5800250
    assert reading.deliveries[0].delivered_on.isoformat() == "2026-10-02"
    assert reading.deliveries[0].lines[0].expires_on is not None
    assert reading.payments[2].method == "cash_to_rep"
    assert reading.returns[0].raised_on.isoformat() == "2026-10-24"
    assert reading.returns[0].status == "pending" and reading.returns[0].value_kobo == 1575000


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_a_source_writes_the_same_book(name: str, tmp_path: Path) -> None:
    """Both shapes land the same rows in the store, and the second import of
    either changes nothing."""
    engine = make_engine(f"sqlite:///{tmp_path / 'book.db'}")
    ensure_schema(engine)
    first = import_book(engine, SOURCES[name].path, tenant_id=TENANT)
    assert sum(c["new"] for c in first.counts.values()) == sum(EXPECTED.values())
    again = import_book(engine, SOURCES[name].path, tenant_id=TENANT)
    assert all(c["new"] == 0 and c["rejected"] == 0 for c in again.counts.values())


def test_every_source_reads_its_own_fixture() -> None:
    """The suite's own guard: each entry is checked against the file it names.

    A third source wired to another source's fixture would pass every comparison
    above while testing nothing, so the file each entry reads has to be its own,
    and reading it again has to produce that entry's reading.
    """
    paths = [source.path for source in SOURCES.values()]
    assert len(set(paths)) == len(paths)
    for name, source in SOURCES.items():
        assert source.path.exists(), name
        assert canonical(Source(name, source.path).read()) == canonical(source.read())


def test_the_two_shapes_are_not_the_same_file() -> None:
    """A guard on the suite itself: if the shapes became one file (a symlink, a
    copy), the comparison above would pass without testing anything."""
    csv_reading = SOURCES["csv folder"].read()
    xlsx_reading = SOURCES["xlsx workbook"].read()
    assert csv_reading.source == "csv" and xlsx_reading.source == "xlsx"
    assert csv_reading.digest != xlsx_reading.digest


def test_a_source_may_differ_only_in_provenance() -> None:
    """What a source is allowed to decide: where a row came from, nothing else."""
    csv_reading = SOURCES["csv folder"].read()
    shifted = replace(csv_reading, label="elsewhere")
    assert canonical(shifted) == canonical(csv_reading)
