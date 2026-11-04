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

Two things the review found and this branch does not fix, both about two
readers and writers on the same invoices at once. Neither can happen in a demo
walked by one person, and both want tests, so they are the first items after
the demo rather than a late change:

1. **Row locking on the receivable writers.** `apply_credit` and
   `_reverse_applications` read an invoice and then write an absolute `paid`
   and `outstanding`. So does Go's own writer for a verified bank payment. Two
   of them can read the same invoice before either commits and the later
   commit then overwrites the earlier one, the classic lost update this branch
   introduces by adding a second writer. The fix is `with_for_update()` on the
   invoice rows read inside the same transaction, and it has to be taken in
   one consistent order (the invoice read order is already `position` then
   `delivery_id`, so the lock order should follow it) to keep two credits on
   one shop from deadlocking.
2. **One snapshot for the exposure read.** `GET /v1/credit/exposure` reads
   payments and receivables separately (and on two connections), so a bank
   credit committing between them shows the same money as both verified and
   outstanding for one request. One connection is not enough by itself under
   READ COMMITTED: the two sides want a single snapshot, so read them in one
   transaction at a repeatable-read isolation level or from one statement.

