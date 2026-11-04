# Returns and credit exposure (3 Nov 2026)

A distributor's returns cost him most: goods are picked back up at his cost,
so he pays twice, delivering and then collecting. `mimi/stock/slow.py` and
`mimi/stock/swap.py` already raise the alarm (slow and near-expiry lines,
with move, promo or pickup options). This is the treatment: the return
process, the settlement and the exposure figure an owner can hold.

Two rules from the rest of the product hold here. Every figure comes from
code, never a model, and money is integer kobo. Mimi never moves stock or
money: the rep collects, and the owner approves every write and every
settlement, with an undo.

## The returns ledger (`mimi/returns/ledger.py`, `mimi/api/returns.py`)

A return opens from an **approved pickup option** on a slow or near-expiry
line: it carries the shop, its lines, its reason, and the quantities and
naira all computed in code. Approving a pickup in the console opens the
draft itself, so nothing is left dangling.

The lifecycle, each move written to `return_events`:

| Status | What happened |
|---|---|
| `drafted` | opened from an approved pickup (or with named lines), waiting on the owner |
| `approved` | the owner approved the pickup |
| `collected` | the rep collected the goods; quantities recorded |
| `inspected` | what came back and its condition recorded per line, reconciled against what was collected |
| `settled` | the credit note was approved and applied against the shop's balance |
| `cancelled` | the owner declined the draft (terminal, nothing moved) |

A line keeps three quantities, so the console never has to guess:

* `quantity_drafted`: what the pickup said would come back;
* `quantity_collected`: what the rep actually took (a line may come back
  short);
* `quantity_returned`: what inspection accepted, with its `condition`
  (`resellable`, `damaged`, `expired`) and its `disposition` (`resell`,
  `write_off`, `return_to_supplier`).

Inspection can never record more than was collected, and every line must be
named (both 409s). A line that came back `damaged` or `expired` cannot be
disposed of as `resell`, because a resold line is counted as recovered and
goods like that cannot go back on the shelf: the choice is write off or send
it back to the supplier. Collection names each line once and only lines that
are on the return (409s), and a return is collected once because the only move
out of `collected` is inspection.

### Expiry and batch from the distributor's own invoice

Shop counts are hard to get and the distributor resists them, so expiry and
batch come off his own invoice line (`delivery_lines.expires_on`, `batch`)
whenever it carried one, and the line says so: `expiry_source` is `invoice`
when the note had the date, `estimated` when it did not. The API never
claims a date off a shop count.

A pickup line names the delivery its goods came in on as far as the book can
say: the plan carries the expiry the pickup is about (the earliest expiry
among the units left), and the line takes the batch of the delivery note that
carries that same expiry rather than the newest note's. When no note carries
that date, the plan's date stands and the batch is the newest note's.

### What the return saved or cost

Optional and configured by the owner
(`GET`/`PUT /v1/returns/settings`, `pickup_cost_kobo`). When it is set, a
return reports:

* `recovered_kobo`: the value off the lines that sell again or go back to
  the supplier; a write-off recovers nothing (a real cost, not a saving);
* `saving_kobo`: `recovered_kobo` less the pickup cost, with
  `saving_basis` stating the assumption in plain words.

When no pickup cost is set, `saving_kobo` is null and no saving is claimed.
The number is only as good as the stated assumption, so the assumption
travels with it.

## Settlement (`return_credit`, the Zoho card shape)

`POST /v1/returns/{id}/settle` drafts a credit, but changes nothing. It opens
a **pending `return_credit` approval** that behaves exactly like the Zoho
payment card: `POST /v1/approvals/{id}/approve` records it, and
`POST /v1/approvals/{id}/undo` takes it back.

Approving it takes the credit off the shop's open invoices, oldest first,
and records every application in `credit_notes` (with the invoice's previous
paid and outstanding figures), so the undo restores exactly what it changed.
When the shop owes nothing, `applied_kobo` is zero and the rest is
`on_account_kobo`: held as credit on the shop's account, never invented as a
payment. The return becomes `settled`; an undo puts it back to `inspected`
so it can be settled afresh.

Three rules keep the card and the return honest:

* Settling again with the same amount hands back the card that is already
  waiting. Settling with a different amount replaces it (the old card reads
  `superseded`), so the card's amount and the return's never disagree.
* A card the owner rejected, withdrew or superseded is not the return's
  credit any more: the return reads no credit waiting, and the next settle
  drafts a fresh one. A rejection never blocks settling.
* The undo takes this credit's own applications back off the invoices by
  arithmetic: `paid` less what the credit put on that invoice, `outstanding`
  plus it. That is the right figure however the invoice has moved since, so
  two credits on one invoice can be undone in either order and a verified bank
  payment landing in between survives the undo instead of making it impossible
  (Go's own system writes the same columns: `demo_wiring._apply_to_invoices`,
  and `week.go.GoSim.open_day` with `GoSim._pay`).
* The invoice's word is recomputed from the balance the undo leaves and its
  due day, the same rule Go's morning roll uses. An invoice that went overdue
  while the credit was on it keeps that word rather than getting a stale one
  back, and no money-inert change can leave the credit stuck with no way to
  reverse it.
* The one case the undo refuses is an invoice that is no longer on the book:
  there is nothing to take the credit back off, so it says so and changes
  nothing.

Undoing the **pickup** itself also reaches the ledger: a return opened from it
that has collected nothing is cancelled with it, and no return can be opened
or walked from a pickup the owner called off. A return that already collected
goods keeps its own record, because something moved.

## Credit exposure (`mimi/credit/exposure.py`, `GET /v1/credit/exposure`)

What one shop could cost, aged and risk-weighted, per shop and in total.

* `outstanding_kobo` and `overdue_kobo` are the money owed, from the book;
  they are the primary figures.
* The ageing bands come from `days_overdue`, the one definition of overdue
  (`mimi/owed.py`), and are returned with the figures so no surface keeps
  its own copy:

| Band | Days overdue | Weight |
|---|---|---|
| `current` | 0 | 100 percent |
| `days_1_30` | 1 to 30 | 125 percent |
| `days_31_60` | 31 to 60 | 150 percent |
| `days_over_60` | 61 or more | 200 percent |

* `exposure_kobo` is the money weighted up by age. The weights are an
  assumption, not money anyone owes, so they are always shown beside the
  figure, under a versioned policy (`exposure-v0.1`).
* Only bank-verified payments count, the rule `mimi/credit/review.py`
  already follows: a payment the shop reported, with no bank credit, never
  lowers exposure. It is reported as `shown_not_counted_kobo`.
* `confidence` is defined, never a feeling. It measures how much the bank
  feed backs the reading, from the count of bank-verified payments in the
  last 8 weeks: 8 or more is `high`, 4 to 7 is `medium`, fewer than 4 is
  `low`. The total's confidence is the weakest shop's with exposure, because
  one unbacked shop is the one that can surprise the owner.

## Tests

`tests/test_returns.py` and `tests/test_exposure.py` (code only, no model):

* the lifecycle, the three quantities, the settlement against a balance and
  its undo, and the credit-on-account case for a shop that owes nothing;
* the settlement rules: a rejected card leaves the return settleable, a second
  settle with a new amount replaces the waiting card, a card whose application
  failed after approval can be settled again, undoing the pickup cancels the
  draft, and the undo reverses the credit's own applications so it stays right
  when a bank payment or a morning roll moved the invoice first;
* the unhappy paths: a pickup with no approval, a non-pickup plan, an
  inspection that disagrees with what was collected, a settlement above the
  return value, a transition out of order, settling with no shop and no
  lines, a double settle and a double undo;
* the exposure bands and weights, the confidence rule, verified versus
  reported payments, and the tenant filter;
* two person-read-string checks: no internal id anywhere a person reads, and
  no em or en dash.

```bash
META_API_KEY= python -m pytest tests/test_returns.py tests/test_exposure.py -q
```

`tests/test_receivables_locking.py` covers the row locking across the writers
of a shop's invoices (see below). Six of its eight tests need row locks, so they
run only where `MIMI_TEST_DATABASE_URL` points at Postgres and skip on SQLite
with the reason in the skip message. The file is named in the Postgres job in
`.github/workflows/ci.yml`, so those six run in CI and not only on a machine
where someone happens to point the variable at a database:

```bash
MIMI_TEST_DATABASE_URL=postgresql://... META_API_KEY= python -m pytest \
    tests/test_receivables_locking.py -q
```

The frozen field names and status strings the console builds against are in
[`returns-exposure-contract.md`](returns-exposure-contract.md).

## Not built (stated plainly)

* No credit note reaches Zoho Books. Settlement adjusts the book's
  receivables and records a `credit_notes` row; Zoho is a separate write
  path that is not wired for credits.
* The pickup cost is one figure for the whole distributor, not per area or
  per shop.
* A return opens only from a slow-stock pickup or with named lines; there is
  no shop-initiated return request yet, and no partial-line reason per
  disposition beyond the line's own reason.

## Row locking on the invoices, and why it is in the read

Three things write a shop's `paid_kobo`, `outstanding_kobo` and `status`: a
return credit (`apply_credit`, `_reverse_applications`), Go's own writer for a
verified bank payment (`demo_wiring._apply_to_invoices`, and
`week.go.GoSim._pay`), and the routine. Each of them has to read the invoice,
work out the new figures from that read, and write those figures back, so a
reader that holds nothing can have its figures overwritten by another writer
that read first and committed later. Nobody sees an error: the balance is just
wrong afterwards. That is why this is the first item after the demo and not a
later tidy-up.

Built (branch `feat/returns-locking`): every one of those reads goes through
`mimi/store/invoices.py`, which takes the rows `FOR UPDATE` inside the caller's
transaction, always in one order, `position` then `delivery_id`:

* `open_invoices(conn, tenant, shop_id, lock=True)` for a shop's open invoices,
  oldest first. `lock=False` is for the draft (`settle`), which only shows the
  owner a plan.
* `invoices_by_id(conn, tenant, delivery_ids, lock=True)` for the undo, which
  reads the invoices a credit came off by id. It takes all of them, in that
  order, before it writes any, so an undo cannot interleave with another writer
  on the second invoice.
* `overdue_ids(conn, tenant, day_no, lock=True)` for the day roll, which marks
  past-due invoices with one bulk `UPDATE`. A bulk update takes the rows it
  touches itself, in whatever order the planner picks, so the roll would be the
  one writer that does not queue in the house order and could cycle with one
  that does. It reads them through here first, holding them, and then updates
  exactly the ids that read returned: each statement takes its own snapshot, so
  a row that starts matching in between would otherwise be taken unheld, and the
  rows the read returned cannot change under it because it holds them.
* `LOCK_ORDER` is the one definition, and the order in the statements comes from
  it (`order_by(*(s.receivables.c[name] for name in LOCK_ORDER))`), not from a
  second literal that could drift from it. A new writer (the routine) calls the
  read instead of writing its own query, which is what keeps a second order from
  appearing and a deadlock from cycling on it.

**Why the read and not the write.** Two reasons, and the second is the one that
decides it. The write needs figures only the read has: which invoice a credit
comes off first, how much each takes, and the status each is left with, since
the credit is planned oldest first, recorded on the `credit_notes` row so an
undo can subtract exactly what it put on, and shown to the owner before it is
applied. But even if the write did not need them, `UPDATE ... SET outstanding =
outstanding - :x` is not a shape this code can use: the amount each invoice
takes is the outcome of a plan (`returns.ledger.credit_plan`) that a person
approves, and the same plan is what the undo reverses. Taking the lock with the
read keeps one place where the figures are decided, and it makes the write a
plain absolute assignment of figures that were read under the lock.

What that buys, proven by tests in `tests/test_receivables_locking.py` (Postgres
only for the ones that need row locks, and they skip on SQLite with the reason
in the skip message):

1. A locked read waits for the writer holding the row, and a read that does not
   ask for the lock returns the figures from before that write at once.
2. A second writer on one invoice waits for the first (here a bank payment
   holding its transaction), then both allocations land: `paid` is the sum of
   both, `outstanding` is the original less both, and `paid + outstanding ==
   total`.
3. The same the other way round: the credit is applied and held, the bank
   payment waits, and the invoice ends with both amounts off one figure.
4. A credit spanning two invoices waits on the first and holds nothing, so
   another writer on the second invoice is not blocked by it. That is the
   property that makes a cycle impossible: a transaction never waits for a
   lower row while it holds a higher one, because every writer takes the rows
   in one order. A transaction may hold a lower row and wait for a higher one,
   which is what this test shows.
5. Two credits over several invoices at once both land, and every invoice keeps
   `paid + outstanding == total`.
6. The same two amounts in either order (a credit and a bank payment) leave the
   same balance, first the payment then the credit, and the other way round.
7. The statement itself: one order (`ORDER BY receivables.position,
   receivables.delivery_id`) with `FOR UPDATE` on the locked reads (including
   the roll's overdue read) and without it on the draft's read.
8. The day roll waits for a writer holding one of the invoices it is about to
   mark overdue: the roll's bulk `UPDATE` is preceded by the same ordered,
   locked read, so the roll queues the way every other writer does.

Removing the lock from `mimi/store/invoices.py` fails six of these eight, every
run: 1 and 4 on their waiting assertions (the second reader gets in at once), 2
and 3 on the lost update (the invoice ends with one writer's figures instead of
both), 7 on `FOR UPDATE` falling out of both locked statements, and 8 on the
roll walking past the invoice the test is holding. The other two do not: 6 is
the plain arithmetic in either order, and 5 is the two-credit race, which fails
only when the two transactions interleave that way. A reviewer measured 5
failing three times of three on Postgres 18.6 (one credit lost each time) and
three runs here passed, which is what a race is. A guard that only fails on some
interleavings is the reason the whole file is named in the Postgres job in
`.github/workflows/ci.yml`: with `with_for_update()` deleted, the offline job
skips these tests and the job that does run Postgres was not running this file,
so both stayed green. The Postgres tests need a database:
`MIMI_TEST_DATABASE_URL=postgresql://... python
-m pytest tests/test_receivables_locking.py`.

One thing still not built: **one snapshot for the exposure read.**
`GET /v1/credit/exposure` reads payments and receivables separately (and on two
connections), so a bank credit committing between them shows the same money as
both verified and outstanding for one request. One connection is not enough by
itself under READ COMMITTED: the two sides want a single snapshot, so read them
in one transaction at a repeatable-read isolation level or from one statement.


