# Proof levels: verified, likely, estimated (RAI-295)

Every record Mimi keeps says how sure Mimi is about it, in one word. The
distributor and the shop see the word beside each figure, never colour alone:

> ₦45,000 paid 12 Oct, likely (rep and shop)

Credit only ever counts **verified** records.

## Where it lives

| Piece | File |
| -- | -- |
| The type, the witnesses, the one rules function, the credit door | `mimi/provenance.py` (a stdlib-only leaf, so the store, credit, stock and the API can all import it) |
| Columns `proof_level`, `witnesses`, `proof_policy_version` on `payments`, `deliveries`, `payment_proofs`; tables `proof_events`, `proof_asks` | `alembic/versions/0010_proof_levels.py`, `mimi/store/schema.py`, `mimi/proof/ledger.py` |
| Adding a witness to a book record, asking once, daily counts by level | `mimi/store/proofbook.py` (`SqlProofBook`) |
| Payment screenshots: likely on a passed check, verified when the bank credit lands, back to estimated when the bank window closes with nothing | `mimi/proof/flow.py` |
| Credit reads verified only | `mimi/credit/review.py` through `verified_records`; architecture test in `tests/test_proof_levels.py` |
| The API | `GET /v1/shops/{shop_id}`: every payment and delivery has `proof_level`, `proof` (the word) and `proof_policy_version` |

## Rules, policy v1

`proof_level(record_type, witnesses, policy) -> level`. Pure code, no model.

| Record | Verified | Likely | Estimated |
| -- | -- | -- | -- |
| Payment by transfer | Matched on the distributor's bank feed (or a provider webhook) | Alert screenshot read and passed the fake check, no bank feed yet | The shop or rep says it was paid, nothing else; or the bank window closed with no credit |
| Payment in cash | Rep log + shop YES + the cash later seen banked | Two of those three | One of them only |
| Payment by POS | The distributor's POS settlement report matches | Slip photo read, no report yet | Claim only |
| Payment to another person's account | That account tagged by the distributor **and** the credit seen on its feed | Not tagged yet, but on a feed or a screenshot passed the check | Claim only |
| Delivery | Distributor record + shop YES + a matched payment | Two of those three | One only |
| Stock figure | POS item data | An owner-confirmed book read | A one-tap, typed or voice count |
| Sell-through period | Every count behind it is POS data | Every count is POS data or a confirmed book read | Anything else |
| Stock estimate | never | never | always |

Witness kinds (each points at the row it came from): `rep_log`,
`distributor_record`, `distributor_tag`, `shop_yes`, `shop_claim`,
`shop_photo`, `alert_screenshot_checked`, `pos_slip_read`,
`book_read_confirmed`, `shop_count`, `bank_feed`, `provider_webhook`,
`pos_report`, `payment_matched`, `estimator`, and the negative
`bank_no_credit`.

### Judgement calls for Tobi to review

1. **Rep log and distributor invoice are one witness**, not two (the open
   question in the issue; the proposed answer). Both are the distributor's
   word. So a rep log plus an invoice stays estimated.
2. **Transfer: rep plus shop with no screenshot and no bank is estimated**,
   following the table ("Shop or rep says it was paid, nothing else"), not
   the general "two witnesses is likely" count.
3. **Cash needs all three for verified.** A bank deposit on its own does not
   verify a cash payment; it has to be tied to the rep's log and the shop's YES.
4. **Levels go down too.** A screenshot payment whose bank window closes with
   no credit goes from likely to estimated, as an event.
5. Existing book rows were labelled from what the book holds: matched on the
   bank feed is verified; cash the rep logged is **estimated** (no shop YES
   is recorded yet); a fake alert is estimated; a delivery is estimated until
   the shop's YES (`witnessed_at`).

## Old labels

| Old | New |
| -- | -- |
| `verified` | verified |
| `checked`, `supplier_document` | likely |
| `self_reported`, `unverified`, `derived` | estimated |
| `incomplete` | stays a separate "not computable" state |

`legacy_level(label)` raises on anything else, so nothing is left unlabelled
by accident.

## Events and asking once

* Every witness that lands writes one `proof_events` row (record, old level,
  new level, the witness, the policy, the time with its zone). A replayed
  witness writes nothing. Rows are never edited.
* A shop is asked to confirm a record once (`proof_asks`). Skipping it never
  lowers the level and is never chased.
* A rule change bumps `POLICY_VERSION`; `restamp` recomputes and writes an
  event. Roll back by pinning the previous version.

## Pass marks (CI, blocking)

`tests/test_proof_levels.py`: every row of the rules table gives its level;
no likely or estimated record reaches credit (architecture test plus a
credit file built from a likely payment); every record is labelled after
the migration and after seeding; every level change has a `proof_events`
row.

## Rollout

Quiet mode first: the levels are computed and stored, shown in the API, and
not yet put in any shop or distributor message. `SqlProofBook.level_counts`
gives the daily count by level and the share verified.
