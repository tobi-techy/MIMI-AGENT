# Order intake eval (RAI-302)

Synthetic orders from shops of the Coral Lane Trading world, run through the
real API agent (front door, router, steer stage, inbox, harness loop on Muse
Spark, the completing transaction) on a fresh in-memory store for each case.

* `heldout.jsonl`: 47 cases, written and committed before the Order intake
  helper existed. Never used for tuning; the report is from one recorded run.
* `dev.jsonl`: 16 cases on other shops and other wording, for tuning only.
  `dev.report.json` and the dev caches are the tuning record from before the
  rebase on main (PRs #16 and #17 changed the core prompt), so the dev set no
  longer replays; it is not run in CI.
* Two held-out labels were fixed from the book before the first held-out run
  (commit "two held-out label fixes"): h33's repeat order is over available
  credit, and h28 moved to a shop with no noodles history.
* `photos/`: synthetic handwritten order lists drawn by
  `python -m mimi.orders.fixtures` (script typefaces on ruled paper, no real
  handwriting, shop or person). `photos.jsonl` says what each page says.

Case kinds: typed English, Pidgin, Yoruba-flavoured English (with and without
tone marks), voice-note transcripts, list photos, unclear item or size (one
question, then the answer), repeat orders, over available credit, pay before
delivery, edits before approval (change, remove, add), edits after the owner
approved, a message that arrives while the order is being worked on, a
multi-part message, a price question, an item the distributor does not carry,
and an injected instruction.

How a case is scored (in code, from the store after the case ran):

* `order`: the shop's newest pending order has exactly these (sku, quantity)
  lines. `repeat` means the lines of the shop's most recent delivery in the
  book; `repeat_plus` adds lines to that.
* Every priced line must use the catalogue price for the live day, line totals
  must be quantity times price, and the total their sum. A line whose sku is
  not in the catalogue, a price that is not the catalogue's, or a quantity
  that is neither in the shop's words nor in the book is an invented figure.
* `question`: Mimi asked one question and made no order this turn.
* `over_credit`: the order is flagged over available credit (limit minus
  open receivables and other pending orders), always waiting in approvals.
  No intent may set or raise a credit limit (credit extended).
* `version` / `superseded`: an edit made a new version and the old pending
  one is superseded and cannot be approved.
* `decided_unchanged`: an approved order is byte-for-byte what it was before
  the shop's later message (an edit to a decided order).
* `new_order`: additions after approval become a new, separate order.
* `also_answered`: the other part of the message got its own reply from that
  helper or skill.
* `price_reply`: no order, and the shop's reply carries the catalogue price
  of one of these skus.

Pass mark 90% of held-out cases fully right. Held at zero: invented prices or
items, credit extended, edits to decided orders.

## Results

After RAI-304 (photo look in the router, 28 Oct 2026): 47/47 fully right,
invented prices or items 0, credit extended 0, edits to decided orders 0.
h25, h26 and h27 now go to Order by the photo rule; the other 44 cases
replayed from the recorded answers unchanged. Live cost of that rerun N90.08
(4 vision, 1 router and 8 loop calls). The router change was tuned on
`eval/photo_route/dev.jsonl` and scored on a fresh set, not on h25 to h27.

### Before RAI-304 (one recorded live run, 28 Oct 2026)

Held-out: 44/47 fully right (93.6%), invented prices or items 0, credit
extended 0, edits to decided orders 0 (`heldout.report.json`). The three
misses are list photos the router did not send to Order (captions "abeg bring
this list", "my list", "order for this week"): no wrong order was made, the
shop got a question or a holding line instead. Live cost of that run N639.46.
