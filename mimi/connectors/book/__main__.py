"""``python -m mimi.connectors.book <folder-or-workbook> --tenant-id <id>``.

    # what would land, and every row that would not
    python -m mimi.connectors.book data/sunline --tenant-id sunline --dry-run

    # the same import, written
    python -m mimi.connectors.book data/sunline --tenant-id sunline

The database is ``--database-url``, else ``DATABASE_URL``, else the store's own
default. A dry run writes nothing at all, not even the import's ledger row.

The exit code is 0 when the file could be read and imported (refused rows are in
the report, not the exit code), and 1 when the file itself was refused: a file
nobody can read is not an import anyone can act on.
"""

from __future__ import annotations

import argparse
import sys

from mimi.connectors.base import ConnectorError
from mimi.connectors.book.writer import import_book
from mimi.store.db import database_url, make_engine


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.connectors.book")
    ap.add_argument("path", help="a folder of one CSV per table, or one .xlsx book")
    ap.add_argument("--tenant-id", required=True, help="the distributor's own id")
    ap.add_argument("--dry-run", action="store_true",
                    help="preview every count and reject; write nothing")
    ap.add_argument("--as-of", default=None,
                    help="the book's day (YYYY-MM-DD); default: its newest delivery "
                         "or payment")
    ap.add_argument("--database-url", default="", help="SQLAlchemy URL (default: env)")
    args = ap.parse_args(argv)
    engine = make_engine(args.database_url or database_url())
    try:
        report = import_book(engine, args.path, tenant_id=args.tenant_id,
                             dry_run=args.dry_run, as_of=args.as_of)
    except ConnectorError as exc:
        print(f"import refused: {exc}", file=sys.stderr)
        if exc.next_step:
            print(f"  {exc.next_step}", file=sys.stderr)
        return 1
    print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
