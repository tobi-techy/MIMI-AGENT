# The synthetic world: Coral Lane Trading

The demo runs on one fictional distributor, **Coral Lane Trading**, and 40
fictional shops in and around Ado-Ekiti. Everything is synthetic: places are
real Ado-Ekiti areas and streets, but every shop, owner, brand, phone number,
account number and amount is invented. No real shop or distributor data is in
this repository. The model calls in a live run are real; the data is not.

Code: `mimi/world/`. Fixture:
`mimi/world/fixtures/coral_lane_seed2610.json`.

## Seed and determinism

* Default seed **2610**. `generate_world(seed)` makes every random choice from
  one `random.Random(seed)` in a fixed order and never reads the clock, so the
  same seed always gives the same world, byte for byte.
* `world_hash(world)` is the SHA-256 of the canonical JSON. A test checks the
  committed fixture hashes the same as a fresh generation, so it cannot drift.
* History runs 8 weeks from Monday 31 Aug 2026. The **live day** the simulator
  plays is day 56, Monday 26 Oct 2026, when all 40 shops get a delivery (a
  compressed route day, so the demo shows every persona).

## Realism rules

Shops and places
* Areas: Oja Oba, Ajilosun, Okesa, Basiri, Adebayo, Fajuyi, Oke-Ila, Irona,
  Odo-Ado, Ilawe Road, each with a few real street names.
* Types: provision store, kiosk, mini-mart, mama-put supplier,
  POS-plus-provisions. Names in the local style ("Mama Tolu Provisions",
  "God's Grace Stores", "Bola & Sons Mini Mart"); mostly Yoruba owner names.
* Sizes: 5 big buyers, 11 medium, a long tail of 24 small shops. Credit limits
  follow size. Two reps split the areas.
* Phones use `+2340000…`: a 0 straight after +234 is never assigned, so no
  synthetic number can reach a real person.

Products and prices
* 30 SKUs at real Nigerian pack sizes (noodles by the carton of 40, sachet milk
  by the bag of 10 and carton of 210, 1L and 5L oil, 5kg and 50kg rice, 1kg and
  50kg sugar, seasoning cubes, detergent, beverages, spaghetti and more).
* Brands are invented (Sunrise, Okin, Palmside, Ekiti Gold …) so the demo says
  nothing about any real brand.
* Prices are rough late-2026 trade prices, in kobo. From day 28 (28 Sep 2026)
  11 SKUs rise by about 8%, rounded to N50 like a real price list.
* Bulk SKUs (50kg bags, 5L kegs, cartons of tins) go to big and medium shops.

Behaviour
* Restock: big and medium shops weekly, small shops every two weeks. No Sunday
  deliveries.
* Ado market comes round every 5 days: big and medium shops top up the day
  before, and quantities on and before market day are about 1.5x.
* End of month (25th to 2nd): about 1.2x quantities for the salary bump.
* Payments by habit: on-time shops pay in 0 to 3 days; late payers promise a
  day ("I will pay on Friday") and miss it about two times in three, then pay a
  round number (N50,000 against a N53,450 bill) and sometimes the rest later;
  disputing shops pay a round-number part.
* Methods: cash to the rep (more common for small shops) or transfer from
  one of six fictional banks: Lagoonpay, Harmattan MFB or Kestrel Bank (app
  banks), or Ironleaf Bank, Ashgrove Bank or Marlwood Bank (SMS banks). Every
  bank name in the world is invented; none is a real bank. About a quarter of
  large on-time transfers are split across two transfers.
* Two historic disputes ("I didn't collect the 2 cartons").
* Exactly 4 fake transfer alerts, each with telltales: the narration in the
  wrong format for that bank, an amount that never appears in Coral Lane's
  bank feed, and often an odd timestamp (2 to 4 am).
* Alerts follow the usual layouts (SMS debit alerts for Ironleaf, Ashgrove and
  Marlwood; in-app receipts for the app banks) with masked, fictional account
  numbers and session ids that start `999`. The layouts are approximations.

Money
* All money is integer kobo, computed in code. A line is quantity x unit price;
  a delivery is the sum of its lines; a receivable's paid amount is the sum of
  matched payments. A fake alert is `unmatched`, is absent from the bank feed
  and never counts as paid.

Chats (the simulator's shop personas)
* Short casual Nigerian English (not heavy Pidgin), typos, sometimes ALL CAPS,
  "ok", a thumbs up, a `[voice note 0:14]` placeholder, bursts of 2 or 3
  messages.
* Reply timing is decided in code from the persona: most within the hour, some
  hours later, some the next morning.
* A shop whose delivery was short complains and never says YES.

## Stats check

`python -m mimi.world --check` regenerates the world, runs
`stats.check` and exits 1 on any problem:

* every line and delivery total recomputes to the kobo;
* billed = paid (matched, non-fake) + outstanding, to the kobo, and every
  receivable adds up;
* the bank feed matches the genuine transfers one for one; no fake alert is in
  it; the fake alert count is exactly 4;
* aging looks plausible: less than half the book is over 30 days late, and
  something recent is owed.

Seed 2610 today: 40 shops (5/11/24), 30 SKUs (11 with the price rise), 308
history deliveries (about 9 a day around market days against about 5 on other
days), 344 genuine payments (99 cash to rep, 34 split transfers, 153
partial), 129 promises (92 missed), 2 disputes, 4 fake alerts. Billed
N66,484,450, paid N53,924,100, outstanding N12,560,350, of which current
N5,329,350, 1-7 days N5,247,850, 8-30 days N1,396,750, 31+ days N586,400.
These figures are printed by the CLI; re-run it after any generator change.
