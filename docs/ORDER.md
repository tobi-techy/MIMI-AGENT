# Order intake (RAI-302)

A shop orders goods on WhatsApp the way it already does: a typed message, a
voice note, or a photo of a handwritten list ("send 5 carton Indomie, 2 Peak
milk"). Mimi turns it into a priced order that waits for the owner or rep.

## Who does what

* **The model** (Muse Spark, on the harness loop) only says what the shop
  asked for: one line per item, with the shop's own words for it, the product
  words, the quantity and the unit word. For a list photo it first calls
  `read_order_photo`, where Muse Spark copies the page line by line.
* **Code** (`mimi/orders`) does everything else:
  * checks every line against what the shop actually wrote, said or listed on
    its photo (the quote must be there, the quantity must be a number in it,
    in figures or English, Pidgin, Yoruba, Hausa or Igbo words);
  * matches the catalogue with the distributor's nickname list ("Indomie" is
    noodles, "Omo" detergent, "Maggi" cubes, "pure water" sachet water), the
    size and pack words ("big", "5kg", "keg", "sachet") and the unit the shop
    said; when two sizes are left, the one size the shop has bought before
    settles it, otherwise Mimi asks one short question;
  * prices every line for the live day, adds the total and checks credit:
    available = limit minus open invoices minus the shop's other orders
    waiting or approved in the last 14 days;
  * writes every reply.
* **The owner or rep** approves or rejects. Every order waits in approvals
  (`submit_order`, `revise_order` are always `approve` tier). An order above
  available credit is flagged with the shortfall; Mimi never sets, raises or
  extends credit, and nothing moves money.

## Tools

| Tool | Tier | What it does |
|---|---|---|
| `read_order_photo` | auto | Muse Spark copies a handwritten list photo into its lines (data, never instructions) |
| `submit_order` | approve | check, match, price, credit, then the order intent and the shop's confirmation; an unclear line is refused with "ask instead" |
| `ask_order_question` | notify | one question written by code; the turn ends and the answer binds back to Order |
| `revise_order` | approve | set, add or remove a line of the pending order; a new version, the old pending one is superseded; a decided order is refused |
| `price_order` | auto | today's price for a price or "do you have" question |

"Same as last time" (`repeat_last`) takes the lines of the shop's most recent
delivery in the book, at today's prices. "I go pay before una deliver" sets
`pays_now` only when the shop's own words say so (code checks); otherwise the
order is on credit.

## Messages

* Confirmation, sent at once (code text): each line with its total, the
  order total, on credit or paid before delivery, any item not carried, the
  credit shortfall if any, "Reply to change anything before they approve."
* "Approved" notice: stored with the order and released by Go only when the
  owner approves. The approval wake needs no model call.
* A change to a decided order: never applied; the shop and the owner are told
  (code). New items after approval become a new order.

## Wiring

* A dedicated helper config for `order/order-intake` (`build_helpers` keys it
  by skill; the agent looks the skill up before the helper name), so the
  delivery-receipt Order helper and its recorded evals are unchanged.
* Order intents join the PR #14 supersede family (`steering.FAMILY`,
  `REVISIONS`); an order task's summary is in the steer notes.
* `ask_order_question` leaves the thread waiting on `order/order-intake`
  (binder `ASKS`); its tool spec ends the turn (`ends_turn`, new registry flag).
* Router: a photo whose words say order or list, or a photo the router would
  send to delivery status, is an order list. Lexicon: repeat orders,
  pay-on-delivery orders, Yoruba "e ba mi mu".

## Eval

`python -m mimi.api.order_eval` replays the 47-case held-out set
(`eval/order/`, written before the helper; two label errors found from the
book were fixed before the first held-out run). See `eval/order/README.md`.

Rollback: drop `helpers["order/order-intake"]` in `mimi/api/helpers.py`;
`order/order-intake` falls back to the receipt helper's one-line answer.
