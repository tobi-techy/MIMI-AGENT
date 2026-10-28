# Photo routing eval (RAI-304)

Does a shop's camera photo reach the right helper? Above all: a photo of a
handwritten order list goes to Order even when the caption is short, vague
or empty, and nothing that only looks like a list ever does.

* `fresh.jsonl`: 27 cases, written and committed before the router change
  (commit "fresh photo routing set"), scored once, never tuned on.
  14 list photos captioned "abeg run am", "", "see", "this one", "e jọ̀ọ́",
  "oja mi ree", "for monday", "🙏", "Coral Lane abeg", "na wetin I need be
  this", "kaya na", "biko", "the usual and these ones", "ehen".
  13 look-alikes that must not reach Order: two shelf photos, a POS slip
  (twice), a printed invoice from the distributor (twice), a registration
  certificate, a handwritten list of payments captioned "my list", list
  photos captioned "una no bring everything for this list", "I don pay for
  this list" and "this list, your driver was rude to my boy", a payment
  screenshot captioned "my list", and a bank deposit teller.
* `dev.jsonl`: 9 other cases (other photos and words), for tuning only.
* `photos/`: drawn by `python scripts/make_photo_route_fixtures.py` (the
  notebook pages of `mimi.orders.fixtures`, and printed slips and sheets
  with invented banks). `photos.jsonl` says what each one says. Shelf photos
  and the payment screenshot are reused from `eval/credit/shelves` and
  `eval/proof/screens`.

Each case goes through the real router (rules, phrases, the fast
classifier, Muse Spark) with the photo look the agent wires in: the Order
list reader on Muse Spark vision over the photo bytes. Every model answer is
recorded and replayed in CI.

Scoring, in code: a list case is right when it is routed to
`order/order-intake`. A look-alike is right when it is not routed to Order
and lands on one of the skills its case allows, or asks one question.
Pass mark 90%; held at zero: a look-alike routed to Order.

## Results (28 Oct 2026)

| fresh set | right | lists to Order | look-alikes right | look-alikes to Order |
| --- | --- | --- | --- | --- |
| words only (the router before RAI-304, `--words-only`) | 14/27 | 1/14 | 13/13 | 0 |
| with the photo look | 27/27 | 14/14 | 13/13 | 0 |

Dev set with the photo look: 9/9 (first run, no tuning needed).
Live cost: fresh N181.55 with the look, N43.07 words only; dev N71.19.

Known gap: when vision says a photo is not a list and the caption is short,
the router's clarifying question can offer odd pairs ("who Mimi is or a
payment screenshot"). It never orders, but the buttons could be better.
