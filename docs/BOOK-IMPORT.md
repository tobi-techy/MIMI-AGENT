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
  returns.csv         return_id, shop_id, raised_on, status, reason;
                      optional rep_id, delivery_id, total_naira
  return_lines.csv    return_id, sku, quantity, unit_price_naira;
                      optional line_no, reason
```

The first eight tables are required. **The two return tables are optional and
come together**: an export taken before returns were recorded has none, and a
re-import of that same book is still one book. A returns file with no lines, or
a lines file with no returns, is refused with one sentence, because a return
with nothing to add up is not a return.

Or any of those tables as sheets of one `.xlsx`/`.xlsm` workbook, named after
the table. Columns are matched by name, not position (`Shop ID`, `shop_id` and
`SHOP-ID` are one column); an extra column is ignored.

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
* **Phones are stored in E.164**, the form the channel sends: `0803 123 4567`,
  `08031234567` and `+234 803 123 4567` are one line, and the store matches an
  inbound message on the exact string (`SqlGo.directory`). The import normalises
  once, keeps the raw form on the canonical row (`raw_phone`) and says in the
  report which numbers it rewrote. A number too short to be a line, or one that
  starts with 0 and cannot be made international, is a reject.
* **A delivery is written whole or not at all.** One unreadable line refuses the
  whole delivery, because a total summed over the lines that happened to read is
  the wrong number wearing the right shape.
* **A return's value is summed from its lines**, quantity times price, and a
  `total_naira` the file states is only compared against that sum
  (`total_mismatch`), never used. A return is written whole or not at all, for
  the same reason a delivery is.
* **A return's status is the returns ledger's own vocabulary**, from the closed
  list (`drafted`, `approved`, `collected`, `inspected`, `settled`,
  `cancelled`, `mimi.types.RETURN_STATUSES`): the book's export says which stage
  the return has reached and the row lands there, with the same transition table
  keeping the order from that point. A status this book does not know, including
  any second vocabulary for the same stages, is a reject (`bad_status`), never
  stored as written or mapped to a guess.
* **A return's reason is from the closed list** too (`shut`, `no stock needed`,
  `refused`, `damaged`, `expired`, `wrong_sku`, `mimi.types.RETURN_REASONS`): the
  visit-record contract's list plus the three only a return has. A free-text
  reason cannot be counted, so it is refused (`bad_reason`).

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
| `bad_phone` | a number too short to be a line, or a leading 0 that cannot be made international |
| `bad_method` | a payment method the book does not know |
| `no_lines` | a delivery with no lines, or with a line that cannot be read |
| `unknown_shop` / `unknown_sku` / `unknown_rep` | a row points at something the book does not have |
| `unknown_delivery` | a line, receivable or payment names a delivery the book does not have |
| `mixes_two_shops` | a receivable or payment names a shop its delivery did not go to |
| `total_mismatch` | a receivable or the file's own delivery total disagrees with the lines |
| `dup_in_file` | the same id twice in one file (identical: first used; different: neither) — every keyed table, including receivables, payments and returns, plus a repeated `line_no` inside one delivery or return |
| `dup_in_book` | the id is already in the book with different content |
| `bad_status` | a return whose status is not one the returns ledger knows |
| `bad_reason` | a return whose reason is not one of the six the contract names |
| `unknown_return` | a return line naming a return the file does not list |

A file-level problem refuses the file with one plain sentence and writes nothing:
a missing table, a missing column, a table over 10,000 rows, a distributor row
that names another tenant, or a tenant that is the synthetic demo world.

## Provenance

Every imported row is traceable to where it came from, through the store's own
columns and the proof policy's own vocabulary:

* **Deliveries and payments** get the book's proof columns
  (`proof_level`, `witnesses`, `proof_policy_version`) filled by
  `mimi.provenance`: one witness, `distributor_record`, pointing at the row's own
  id. One party's word is `estimated`, so an imported book is `estimated` until a
  shop confirms a receipt or the bank feed matches a payment. That is the honest
  level, and it is what the console shows.
* **Returns carry the same three proof columns.** A return is the distributor's
  own record and nothing else witnesses it (the shop does not confirm a return,
  the bank is not involved), so the policy's own rule (`return`, policy v2)
  makes it `estimated` and leaves it there. The importer computes the level
  through `mimi.provenance.stamp` rather than asserting it.
* **Each of those rows also writes one append-only `proof_events` line**, built by
  `mimi.provenance.add_witness`, the same function that records a witness when one
  lands outside an import. So the record type is one the policy knows — `delivery`
  for a delivery, `return` for a return, and `payment_transfer` / `payment_cash` /
  `payment_pos` for a payment, never a generic `payment`, because that distinction
  decides which witnesses can lift a figure at all — and the level is computed by
  the rules rather than asserted by the writer. `record_id` is the row's own id,
  `witness_source` the import run, `at` the moment it was read.
* **Shops, reps, SKUs, lines and receivables are reference rows the proof policy
  has no record type for**, and the import does not invent one. Their provenance
  is the run: the `connector_sync_runs` row names the file and its digest, the
  moment it was read, and how many rows of each table came from it, and their own
  external ids are their keys. Extending the policy with new record types is a
  policy decision (`POLICY_VERSION`), not something an importer does quietly.
* **The run itself** is that `connector_sync_runs` row (`scope = "book"`):
  `rows_read` is what the source actually read, `rows_mapped` what it wrote, and
  every reject in plain words is in `errors`. It is how "where did this book come
  from" is answered, and a second import of the same file shows `rows_read` 32,
  `rows_mapped` 0 — it read the book and had nothing to write.

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
operator names (a book exported on Sunday still describes Friday).

The day only ever moves forward, and the move is a compare-and-set in the
statement: two imports racing on one tenant cannot lose the later day. An
`--as-of` earlier than the day the book is already on is **refused** with both
days named, because half applying it would store each invoice's word against one
day while overdue is counted from another. Invoice statuses are restated from the
day actually in effect after the rows are written, so the word and the sum agree.

Day numbers stored beside it (`deliveries.day`, `payments.day`,
`receivables.due_day`) are date ordinals, so "days since" arithmetic works on an
imported book exactly as it does on the synthetic one.

## Running it

```bash
# what would land, and every row that would not: nothing is written
python -m mimi.connectors.book data/sunline --tenant-id sunline --dry-run

# the same import, written
python -m mimi.connectors.book data/sunline --tenant-id sunline

# a book whose day is known
python -m mimi.connectors.book data/sunline.xlsx --tenant-id sunline --as-of 2026-10-27
```

The returns an imported book leaves open, aged on the book's own day:

```bash
python -m mimi.store.returns --tenant-id sunline          # the list, oldest first
python -m mimi.store.returns --tenant-id sunline --json   # the typed rows
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
  amendment flow (with its own audit) is a separate design. A return is the case
  that will ask for it first: a later export that moves the same return from
  `drafted` to `collected` is a changed row, and today it is refused like any
  other, because an import must not edit a book it does not own.
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
  unchanged. Plus the cases review found: a phone written three ways, an `--as-of`
  before the book's day, a repeated id in one file, a repeated line number, a
  negative amount paid, a stale plan landing after a newer one, a payment's own
  record type, and the CLI refusing SQLite in production.
* `tests/test_book_conformance.py`: the shared suite. The fixture book
  (`tests/fixtures/books/small`, made by `scripts/make_book_fixtures.py`) in both
  shapes must produce identical canonical rows and the same book.
