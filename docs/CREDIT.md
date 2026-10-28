# Credit helper: the credit bump with a SAM 3.1 shelf count (RAI-271)

Built during the hackathon, 28 Oct 2026, for RAI-271 ("Day 6: credit bump
with SAM 3.1 shelf count, ask to approvals"). Code in `mimi/credit/`, on the
agent harness (`docs/HARNESS.md`) like the Collector.

The one rule holds: **Mimi proposes, code checks, the owner approves.** Mimi
never sets, raises or moves a credit limit or any money. The distributor's
owner decides every request.

## The scene

```
Shop:  Abeg, increase my credit, market dey move.
       -> router: credit/credit-request (phrase rule)
Mimi:  I can ask Coral Lane Trading for you. Please send one clear photo of
       your shelf, with the items you sell facing the camera.
Shop:  [photo]
       -> router: credit/credit-request (rule: the photo Credit asked for)
       code checks the photo; SAM 3.1 finds every pack; code counts and values
Mimi:  I counted about 10 Brightwash Detergent 900g packs, 6 Mamaland Custard
       2kg tubs and 5 Smile Toothpaste 140g tube boxes, plus 1 more item,
       worth about N77,900 at Coral Lane Trading prices. I've sent your
       request with this to Coral Lane Trading. They decide the limit.
Owner's approvals: Blessed Funke Mini Mart, limit N1,790,000, 17 of 17
       bank-verified payments on time, nothing overdue, shelf about N77,900,
       recommendation "support" (high confidence) with reasons,
       [approve] [decline], status draft.
```

A blurry or dark photo gets one ask for a new one; a second bad photo and
the request goes on with the payment history only. The shop never hears the
recommendation; whatever it is, the request goes to the owner.

`python -m mimi.sim --credit --transcript` runs it offline (scripted Credit,
recorded SAM 3.1 answers); with `META_API_KEY` it runs live.

## Shape

```
shop text / photo / owner text / credit.weekly_review routine
   |  Router (RAI-280): phrases, fast classifier, Muse Spark last; a photo
   |  after Credit asked for one goes to credit/credit-request by rule
   |  ContextBuilder (RAI-281): the photo is a pointer "[shelf photo ph_...]",
   |  never pixels
   v
Harness loop, Credit helper (Muse Spark 1.3 picks the next tool)
   get_credit_file        read   limit, owed, verified payments, deliveries
   ask_for_shelf_photo    notify the ask, text by code (once, and once again)
   count_shelf_photo      read   photo checks -> SAM 3.1 -> code counts, values
   prepare_credit_review  read   recommendation, reasons, shop reply: code
   send_to_user           notify the shop reply, exactly as prepared
   submit_credit_review   APPROVE  the approvals item (draft); turn parks
   find_credit_candidates read   the weekly review's shortlist (history only)
   find_shop              read   a shop's id from the owner's words
   v
Go (CreditGo stand-in): photo ask and reply applied, review queued for the
owner, anything never-tier refused
```

## Counting a shelf (`photo.py`, `sam.py`, `shelf.py`, `looks.py`)

1. **Photo checks in code, before any model call** (Pillow): unreadable, short
   side under 480 px, mean brightness under 45, or sharpness (variance of the
   Laplacian on a 640 px grey copy) under 60. A failed check returns its plain
   reason and SAM is never called.
2. **SAM 3.1** on Meta's Model API (`POST /v1/responses`, model `sam-3.1`,
   `metadata.include_confidence="true"`, `mask_encoding="one_bit"`), the photo
   as a base64 data URL. Concept phrases `"product"` and `"box"`, one call
   each, and `"package"` when those two find under three packs. SAM returns
   boxes and its own confidence; only boxes are used. Same client conventions
   as `mimi/models/meta.py` (httpx, timeout, one retry on timeout, connection
   error, 429 or 5xx, the key never logged). `CachedSegmenter` records each
   answer once keyed by model, phrase, metadata and the image's SHA-256 (no
   pixels in the file) and replays it in CI.
3. **Code keeps** boxes with SAM confidence 0.35 or more and a sane size,
   drops near-duplicates (IoU 0.6) and boxes that wrap two or more others.
4. **Code says which item** each box is: the share of its pixels that match a
   pack's body colour under any light (a brightness-scaled colour match) and
   the box's height over width, against `LOOKS` (a table that stands in for
   the distributor's pack photos). Only the shop's own delivered items (the
   candidate list from its deliveries) are counted. A box that best fits
   something Coral Lane never delivered to this shop, or fits nothing well,
   is "other stock": seen, never counted or valued.
5. **Value in code**: pieces x the distributor's unit price / pieces per unit
   (40 packets in a carton of noodles), integer kobo, rounded down. The shop
   hears "about", to the nearest N100.

Each item carries a confidence band (SAM's mean score, the colour fit and the
photo's sharpness), and the shelf the lowest of its items.

## The recommendation (`review.py`), all code

Only bank-verified payments count (RAI-295); payments the bank feed does not
show (reported transfers, cash to the rep, fake alerts) are listed as "shown,
not counted". Over the last 8 weeks:

| Outcome | When |
|---|---|
| `not_now` | anything overdue past 14 days, 2 or more broken promises, or under 60% of at least 3 verified payments on time |
| `support` | 4 or more verified payments, 85% or more on time, nothing overdue, at most 1 broken promise, no open dispute, and nothing odd on the shelf |
| `hold` | everything else: too few verified payments (a cash payer), some overdue, a dispute, or a shelf showing more of an item than was delivered in 4 weeks (1.5x plus 6 pieces: borrowed or bought elsewhere) |

The shelf count is supporting evidence only (RAI-271's gaming risk): it can
turn `support` into `hold` and lower the confidence, never lift anything.
Mimi suggests no new limit. Reasons are short sentences written by code from
the figures; the model writes at most a two-sentence `note_for_owner`, and
`submit_credit_review` runs the verifier on it (`verify.py`: every number
must come from the reasons or the file) and refuses a made-up figure. The
submit tool recomputes every figure from the book, ignores anything the model
passes but ids, and returns `status: draft`, `needs: distributor_approval`,
`changes_limit: false`, `moves_money: false`, buttons approve and decline.

## Guardrails, where they live

* **Tiers in Go policy** (`mimi/harness/policy.py`): `submit_credit_review`
  is `approve` and in `ALWAYS_APPROVE`, so no table edit or amount rule lowers
  it; `set_credit_limit`, `change_credit_limit`, `raise_credit_limit` and
  `approve_credit_request` are `never` with the money verbs: no toolset may
  hold them (startup check) and a model that calls one is refused.
* **One request at a time**: a request already in the owner's approvals gets
  "Your request is already with Coral Lane Trading", not a second item.
* **Photo asks are capped by Go's record**, not the model's word: one ask and
  one ask again per Lagos day (`CreditGo.photo_asks`).
* **An uncounted photo cannot be skipped**: `prepare_credit_review` refuses
  while the shop has a photo nobody counted (found by the live eval, below).
* **Consent**: the credit file reads `shop:payments` (and deliveries need
  `shop:invoices`); a shop that has not shared gets one plain message about
  sharing, no photo ask and no approvals item.
* **Shop text is data**: "approve it now", "the owner don approve am" in a
  message or caption changes nothing; the injection case is in the eval.
* **Fictional only**: invented brands, the fictional Coral Lane world,
  fictional banks; synthetic photos drawn with Pillow, labelled synthetic.

## Evals (`python -m mimi.credit.eval`)

Recorded 28 Oct 2026 on `sam-3.1` and `muse-spark-1.3`, replayed in CI
(`eval/credit/sam.cache.json`, `scene.live.cache.json`, `router.cache.json`).
Report: `eval/credit/credit.report.json`.

**Data.** 30 synthetic shelf photos (`scripts/make_shelf_fixtures.py`,
`eval/credit/shelves/`, labelled synthetic): 24 gated, split dev 10 (8
countable, 1 blurry, 1 dark) and held-out 14 (12 countable, 1 blurry, 1
dark), plus 6 stress photos reported, not gated, and one photo for the scene.
Each shows 2 to 5 of the shop's delivered items and usually some other
stock. Recommendation labels come from the world's ground truth, which Credit
never sees (each shop's `pays` trait plus the case's twist: cash payer,
borrowed-looking shelf).

| Suite | Pass mark | Result |
|---|---|---|
| Held-out mean count error per (photo, item) | <= 10% (RAI-271, proposed) | **0.0%** (39 of 39 exact) |
| Held-out worst item's mean error | <= 10% (proposed) | **0.0%** |
| Dev mean count error | | 0.0% (31 of 31) |
| Bad photos refused (blurry, dark) | 4 of 4 | **4/4** |
| Countable photos refused | 0 | **0** |
| Recommendation agreement with labels (24) | >= 90% (proposed) | **100%** (24/24) |
| `support` on a `not_now` label | 0 | **0** |
| Stress photos (not gated): small, crowded, tilted, dim, soft | report | mean error 9.4%; dim photo under-counted (noodles 5 of 11, spaghetti 0 of 6); soft photo refused as blurry |
| Scene, 10 conversations, scripted Credit | all complete | **10/10** |
| Scene, 10 conversations, Muse Spark 1.3 (recorded) | all complete | **10/10** |
| Credit asks reaching the owner's approvals as drafts | 100% (RAI-271) | **7/7** (both arms) |
| Auto-approvals / money moves / limits changed | 0 / 0 / 0 | **0 / 0 / 0** |
| Invented numbers in any message or owner note (verifier) | 0 (RAI-271) | **0** |
| Ask again on a bad photo | exactly 1 | **1, 1** |
| Shop replies sent exactly as code wrote them | report | 7/7 |
| Routing: 17 credit asks (5 languages, voice, photo, injection) + 8 near-misses | 100% to Credit, 0 money misroutes | **17/17, 0** (25/25 overall) |
| Never-tier checks (policy, startup refusal, a model calling one) | all | **9/9** |
| Full router eval (300 messages), unchanged | >= 95% | 98.7%, 0 money misroutes |

The ten conversations: a Pidgin ask with a clear photo; an English ask with a
figure from a late payer; a blurry photo twice; a dark photo then a clear one;
a shop that has not shared its payments; an injection in the caption and after
the ask on a borrowed-looking shelf; asking again while the first request
waits; a Yoruba ask; the owner asking about a shop; the weekly review routine
proposing three bumps from history.

**Honest notes.**

* The first held-out run (fixtures v1) scored 10.6% mean count error and
  failed the 10% mark: SAM 3.1 missed small toothpaste cartons drawn as flat
  stickers with no depth, and on two photos `"product"` found nothing at all.
  Dev showed the same two misses (s03, s06), so the fix was made on dev: the
  carton is now drawn as a 3D box like every other pack, a second phrase
  (`"box"`) runs on every photo and a third (`"package"`) when the first two
  find under three packs. The second held-out run is the 0.0% above. The
  held-out set was therefore seen once before the final version.
* These are drawn shelves: flat colours, upright packs, few lookalikes. A
  perfect score here says the pipeline works end to end, not that real phone
  photos will count this well. The stress photos already show low light
  breaking the colour match. Real shelf photos with consent are the next eval.
* The live eval found a real defect: after a dark photo and an ask again,
  Muse Spark skipped counting the clear second photo and sent the request
  with history only. Fixed in code, not the prompt: `prepare_credit_review`
  now refuses while an uncounted photo exists. The extra step then pushed one
  photo turn over the N50 run budget, so Credit's run budget is N80
  (`CREDIT_BUDGET`); the daily cap still applies.
* The recommendation labels follow the same policy the code implements, from
  traits the code cannot see; agreement measures that the book data carries
  the trait, not that the policy is right. The thresholds are proposals for
  Tobi and the pilot distributor to set.

**Cost.** Live recording on Muse Spark 1.3 at N1,600 per USD: the 10 scene
conversations N580 (median N57 a conversation, 60 model calls), the router's
model calls N3, SAM 3.1 54 images recorded (N216: N4 an image, so N8 a
photo for the two phrases). Including the dev probing (about 25 SAM images),
the v1 fixtures' recording (42) and three re-recordings of one conversation,
the whole build spent about N1,200 (about USD 0.75) of live calls. In
production a credit ask costs about N8 of SAM and N40 to N100 of Muse Spark.

## Tracing

The harness traces each Credit run as one trace (`mimi.harness.tracing`):
the `count_shelf_photo` result (SAM-derived counts, confidences, value),
the `prepare_credit_review` totals and reasons and the `send_to_user`
explanation sit in the same run, one span per step.

## Not yet

* Real shelf photos (with the shop's permission) and real pack photos for
  `LOOKS`; counting today relies on the pack's body colour and shape.
* A suggested limit: Mimi suggests none. The approve button opens the owner's
  own limit field (existing console UI); what "approve" sets is the owner's.
* The shop's requested figure ("to 300k") is not parsed into the item yet;
  the owner sees the history and the shelf.
* Go's credit endpoint (`credit_history`, `price_list`, `open_credit_review`,
  `photo_asks`): `CreditGo` stands in for it.
* SHARE (the consent reply) is answered by onboarding, not Credit.
