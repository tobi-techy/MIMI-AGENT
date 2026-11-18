# Book import: a distributor's own data, in

CSV and XLSX are the first real source for the book. Everything Mimi shows is
arithmetic over shops, reps, SKUs, deliveries and their lines, receivables and
payments, and until this path existed the only writer was
`python -m mimi.store seed` (the synthetic Coral Lane world). This is the path a
real distributor's export lands through.

The rules are `plans/mimi-mvp/book-import-spec.md` (Tobi, 18 Nov 2026) and the
layering is `plans/mimi-mvp/data-architecture.md`. This document says what the
code does, and where it deliberately stops.

## The four rings, and where the code lives

| Ring | What it is | Where |
|---|---|---|
| 1 Sources | one file shape per adapter: a folder of CSVs, a workbook, later Sheets and Zoho | `mimi/connectors/book/csv_source.py` |
| 2 Canonical | typed rows, provenance, dry run, reject report, the writes | `mimi/connectors/book/rows.py`, `writer.py` |
| 3 Verify | the totals summed in code, a file total compared against them | `writer.py` (import time), the reconciliation report (not built) |
| 4 Agent | reads the book, never writes it | unchanged |

A source reads its own shape into the canonical rows in `rows.py` and nothing
else. Everything above it is shared, so a new source adds no rule and no
arithmetic: Google Sheets and Zoho Books plug in at `SOURCES` in
`tests/test_book_conformance.py` and must pass the same suite.

## The two file shapes

```
book/
  distributor.csv     tenant_id, name, owner_phones
  reps.csv            rep_id, name, phone, areas
  shops.csv           shop_id, name, owner, area, street, phone, rep_id,
                      credit_limit_naira, restock_days
  skus.csv            sku, name, brand, unit, price_naira
  deliveries.csv      delivery_id, shop_id, rep_id, delivered_on, minute, total_naira
  delivery_lines.csv  delivery_id, sku, quantity, unit_price_naira, batch, expires_on
  receivables.csv     delivery_id, shop_id, due_on, total_naira, paid_naira
  payments.csv        payment_id, shop_id, delivery_id, paid_on, amount_naira,
                      method, reference
```

Or the same eight tables as eight sheets of one `.xlsx`/`.xlsm` workbook, named
`distributor`, `reps`, `shops`, `skus`, `deliveries`, `delivery_lines`,
`receivables`, `payments`. Columns are matched by name, not position (`Shop ID`,
`shop_id` and `SHOP-ID` are one column); an extra column is ignored.

`deliveries.total_naira` is optional. When it is there it is **compared** with
the sum of the delivery's lines, never used: a file that states a different
figure is refused (`total_mismatch`).

### The rules a row has to hold

* **Money is integer kobo.** A naira cell is multiplied by 100 once, on the way
  in, and has to be exact: `4200.505` is a reject that says it is 420050.5 kobo,
  not a rounded price. `₦`, `N` and `NGN` prefixes are stripped; commas are
  thousands separators.
* **Dates are ISO (`2026-10-26`) or Nigerian day-first (`26/10/2026`).** A sheet
  date cell needs no parsing. `03/04/2026` is 3 April: the book's own convention,
  stated once here so the reader never guesses month-first.
* **Totals are computed.** A line total is quantity times price, a delivery total
  is the sum of its lines, outstanding is total minus paid. None of the three is
  ever read from a file.
* **Payment methods** are `transfer`, `cash_to_rep` (or `cash`) and `pos`.
  Anything else is a reject: the payment vocabulary is the book's, not the file's.
* **A delivery is written whole or not at all.** One unreadable line refuses the
  whole delivery, because a total summed over the lines that happened to read is
  the wrong number wearing the right shape.

## What is refused, and what is *not* refused

Nothing is refused for being a *gap* in the file: a shop with no credit limit is
imported with a limit of zero, which the credit helper reads as no credit granted,
a delivery with no batch has none. A reject is for a row that cannot be read
honestly.

| Reason | When |
|---|---|
| `missing_field` | a key, a name, a phone, a price, a total or an amount is empty |
| `bad_date` | a date neither form can read (`31/02/2026`) |
| `bad_money` | not an amount, or not a whole kobo figure |
| `bad_number` | a quantity that is not a whole number of units |
| `bad_method` | a payment method the book does not know |
| `no_lines` | a delivery with no lines, or with a line that cannot be read |
| `unknown_shop` / `unknown_sku` / `unknown_rep` | a row points at something the book does not have |
| `unknown_delivery` | a line, receivable or payment names a delivery the book does not have |
| `mixes_two_shops` | a receivable or payment names a shop its delivery did not go to |
| `total_mismatch` | a receivable or the file's own delivery total disagrees with the lines |
| `dup_in_file` | the same id twice in one file (identical: first used; different: neither) |
| `dup_in_book` | the id is already in the book with different content |

A file-level problem refuses the file with one plain sentence and writes nothing:
a missing table, a missing column, a table over 10,000 rows, a distributor row
that names another tenant, or a tenant that is the synthetic demo world.

## Provenance

Every imported row carries where it came from, in the store's own columns:

* **Deliveries and payments** get the book's proof columns
  (`proof_level`, `witnesses`, `proof_policy_version`) filled by
  `mimi.provenance`: one witness, `distributor_record`, pointing at the row's own
  id. One party's word is `estimated`, so an imported book is `estimated` until a
  shop confirms a receipt or the bank feed matches a payment. That is the honest
  level, and it is what the console shows.
* **Every imported row** (shops, reps, SKUs, lines and receivables included, which
  have no proof columns) writes one append-only `proof_events` row:
  `record_type` and `record_id` name the row, `witness_kind` is
  `distributor_record`, `witness_source` is the import run, and `at` is the moment
  it was read.
* **The run itself** is a `connector_sync_runs` row (`scope = "book"`) with the
  source label and the file's digest, the row counts, and every reject in plain
  words. It is how "where did this book come from" is answered.

## Idempotency, and what a re-import is for

Every entity is keyed by the distributor's own id. On a re-import:

* the same row with the same content is **unchanged** and writes nothing (no new
  book row, no new proof line);
* a row the book does not have is **added**;
* the same id with **different content** is refused (`dup_in_book`), never
  overwritten, because an import must not edit a book it does not own.

So a re-import repairs a gap (a row the book was missing) and adds new activity;
a *correction* to an existing row needs a path of its own, which does not exist
yet. The columns the book changes by itself (a payment's status and proof level, a
delivery's witness time, a receivable's paid amount) are not read as disagreement.

## The book's day

`tenants.live_date` is the day overdue is counted from. Without `--as-of` it is
the newest delivery or payment in the book after the import; with it, the day the
operator names (a book exported on Sunday still describes Friday). Day numbers
stored beside it (`deliveries.day`, `payments.day`, `receivables.due_day`) are
date ordinals, so "days since" arithmetic works on an imported book exactly as it
does on the synthetic one.

## Running it

```bash
# what would land, and every row that would not: nothing is written
python -m mimi.connectors.book data/sunline --tenant-id sunline --dry-run

# the same import, written
python -m mimi.connectors.book data/sunline --tenant-id sunline

# a book whose day is known
python -m mimi.connectors.book data/sunline.xlsx --tenant-id sunline --as-of 2026-10-27
```

A tenant made by the import is not the demo world: `tenants.simulated` is false,
the shops are false, and the owner phone in the distributor row becomes the
tenant's owner (the person who signs in). An empty book reads as empty, with
zeroes, never as a sample of Coral Lane.

## Deliberately not built (yet)

* **One sheet with an `entity` column.** The spec allows it as an alternative
  shape; a folder of tables and a workbook of sheets are what exist, and the
  conformance suite covers both.
* **A correction path.** A changed row is refused rather than applied. An
  amendment flow (with its own audit) is a separate design.
* **Reconciliation as a report.** The import compares what it can at write time
  (file total against lines, receivable against delivery). A standing
  reconciliation report over an imported book is ring 3 and not in this change.
* **Google Sheets, Zoho Books, MCP, the computer.** Same contract, next work.
* **A console screen for the report.** The CLI prints it; the API exposes the
  ledger row and the rejects in it.

## Tests

* `tests/test_book_import.py`: every rule and every unhappy path (empty file,
  wrong header, missing price, unreadable date, a delivery with no lines, a
  duplicate id whose content changed, a file that mixes two shops, a naira figure
  that is not whole kobo, a file total that disagrees with its lines), then the
  acceptance: the fixture book imported, overdue counted in code and compared
  with a hand count (₦80,052.50), the same file imported twice and the book
  unchanged.
* `tests/test_book_conformance.py`: the shared suite. The fixture book
  (`tests/fixtures/books/small`, made by `scripts/make_book_fixtures.py`) in both
  shapes must produce identical canonical rows and the same book.
