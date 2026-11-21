# The daily brief (MIAG-10, 21 Nov 2026)

The first thing Mimi delivers that a distributor owner pays for: one message a
day, before the gate meeting, that attacks the four problems the research named
(poor visibility, credit exposure, collection discipline, logistics of returns)
over the distributor's own imported book. It is
`plans/mimi-mvp/instinct-model.md` run against the real book.

Code, not a model. `mimi/api/brief.py` counts everything; no model is called
anywhere in this path, so there is nothing to record or replay and no figure can
be invented. Findings are notes on the owner's thread (`mimi/api/routines.py`),
never actions: nothing is written to the book.

## The message

Five sections, in the owner's order of worry, at most eight items across all of
them, and every item names an owner and a next action. The rest are counted,
not listed.

| Section | What it answers |
|---|---|
| What changed | the movement in money at risk, what came in, how many returns closed, against the last brief's own numbers |
| Money at risk | the shops that owe, the shop gone quiet while owing first, then oldest debt first and amount second, with the ageing bands and the account over its limit and still taking deliveries called out |
| Collections today | the biggest exposure by amount times age, each with an expected amount and a named owner |
| Returns | what is still open and aged, anything past two days escalated in words, and a product returned by three or more shops inside a beat flagged as the product's problem |
| Coverage | the shops past their own restock rhythm, by cycles missed, with the rows the book cannot judge grouped apart and never scored |

A section with nothing to say says so in one line ("Returns: nothing is open."),
because a missing section reads as an all-clear. The message is written for a
phone: short lines, no tables, naira as `₦15,750`, days in Africa/Lagos, no
internal id anywhere, and it passes the style guard
(`mimi.harness.style.clean_reply`).

### The judgements, and where they come from

* **Age before size** (rule 1): the money-at-risk list ranks by days overdue,
  then amount. A small debt at seventy days is worse than a large one at ten.
* **Quiet while owing** (rule 3): a shop past its own restock interval with
  money outstanding is listed before every other credit item. The cycles come
  from `mimi/store/attention.py`, the same read the attention list uses; the
  brief does not keep its own copy of that arithmetic.
* **Over limit and still ordering** (rule 2): the day a shop's running balance
  first passed its credit limit is computed by walking its deliveries and
  payments in date order (payments that day first). A delivery on a later day
  flags the line; a shop that is merely over its limit is told so without the
  flag, because the danger is fresh deliveries, not the balance itself.
* **Collections** (rule 4): the target is the top shops by amount times age,
  with an expected amount that is the smaller of what the shop owes and the
  middle of its own past payments, so the number is evidence from the book.
* **Unbanked cash** (rule 5): where the newest payment from a target shop is
  `cash_to_rep` with no bank credit behind it, the item escalates to the
  supervisor of the shop's area, or to the owner where the book has none (an
  imported book carries no supervisors), and the cash is named.
* **Returns** (rules 6 and 7): a return open more than two days is escalated in
  words; a SKU returned by three or more shops inside one beat (14 days) is a
  product problem, because the cause is in the order suggestion, not in three
  shops' luck.
* **Coverage** (rule 9): ranked by cycles missed against each shop's own
  restock interval, never by days; a shop the book cannot judge is grouped
  apart and never scored.
* **Eight items** (rule 10): hard cap. Three money items, three collections,
  one return and one coverage line, in that order.

## The brief's memory

"What changed" needs yesterday's numbers, and the book does not hold them, so
`brief_snapshots` keeps one row per tenant and Lagos day: money at risk, overdue,
what came in, returns open and closed, shops past their rhythm, the items listed
and the collection targets. Alembic `0023`. It is the routine's memory, not the
distributor's book: no receivable, payment or return is touched, and a missing
row (the first brief, or a day the routine did not run) only means that brief
says "this is the first brief" instead of inventing a comparison.

## The 8:30 variance

The morning brief sets each rep an amount. At 8:30 the ageing scan
(`mimi/api/routines.py`, `brief.variance`) posts what came in against it, per
rep: "Rep Kayode Ajayi was set ₦660,000 and ₦50 came in, ₦659,950 short." The
targets are the morning's own collection items, and a collection is a payment on
the book's day from the shop each one named. A day with no brief behind it says
it cannot be measured.

## The eval

`make brief-eval` (`mimi/api/brief_eval.py`) runs twelve hand-marked scenarios.
Every scenario is a synthetic book written as CSV,
imported through the real importer, and read through the real routine at 8:00
Africa/Lagos, so what is marked is the message an owner would receive.

The marks fail when the brief is *wrong*, not when it is missing: the account
over its limit and still ordering is flagged and the merely over-limit one is
not; a shop quiet for four cycles ranks above one with a bigger fresh balance; a
70-day debt outranks one twice its size at 10 days; a return raised three days
ago is escalated; one SKU returned by three shops is a product problem and one
returned by two is not; the eight-item cap holds and the rest are counted
truthfully; a quiet day says so in every section; the same book on the same day
is the same message; and every naira figure in the text is a row's amount or a
sum of rows, read from the database by the eval rather than described by the
brief.

`python -m mimi.api.brief_eval` exits 0 only when every mark passed and at least
one ran (`mimi.evalmarks`).

## What is deliberately not built

* **A dashboard.** The message arrives whether or not anyone remembers to look;
  a console screen for the same numbers is a separate surface.
* **A closed loop on items.** Each item is filed on the thread and the
  end-of-day variance closes the collection loop; tracking whether the owner
  acted on a return or a coverage item, and recording the outcome, is the next
  piece (`instinct-model.md`, "Closed loop or it is noise").
* **Per-distributor thresholds.** The bands (two days, one beat, eight items)
  are defaults in code, named where they are used; the first real distributor
  moves them.
