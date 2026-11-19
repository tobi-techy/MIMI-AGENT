"""The book import (RAI-6): a distributor's own data, in, with every rule checked.

A source reads a file shape and produces canonical rows (``rows``); the writer
checks them against the tenant's book, previews them, and writes them (``writer``).
CSV and XLSX are the first source (``csv_source``); Google Sheets and Zoho Books
land behind the same canonical layer. The rules are in
``docs/BOOK-IMPORT.md``, the fixture they are tested against in
``tests/fixtures/books/small``.
"""

from mimi.connectors.book.csv_source import read_book
from mimi.connectors.book.rows import Reading, Reject
from mimi.connectors.book.writer import Plan, Report, build_plan, import_book, write_plan

__all__ = ["Plan", "Reading", "Reject", "Report", "build_plan", "import_book", "read_book",
           "write_plan"]
