# Simulated week: offline run (seed 273, sqlite)

**PASS**. 1 week(s), 360 turns in 65.4 s of wall time.

| Mark | Result | Pass mark |
|---|---|---|
| Turns complete | 100.0% ({'done': 359, 'merged': 1}) | >= 98% |
| Invented figures sent | 0 | 0 |
| Claims-gate hits (held, not sent) | 11 | counted |
| Duplicate sends | 0 | 0 |
| Replayed message ids returning the first turn | 15/15 | all |
| Lost jobs | 0 | 0 |
| Lease takeovers with no planned kill | 1 | counted |
| Budget breaches (step, time, naira) | 0 | 0 |
| Daily cap breaches | 0 | 0 |
| Never-tier attempts (refused) | 4 | counted |
| Never-tier actions | 0 | 0 |
| Forced worker kills fired (each taken over, completed once) | 6 (6 ok) | |
| Routing (informative; offline router has no model) | 98.8% of 338 | |
| Turn latency p50 / p95 / max | 0.5 / 3.088 / 3.306 s | |
| Cost | spent N0.00; projected at Muse Spark 1.3 prices N7,185.81 (N19.96 a turn) | |

## Skill mix (done turns)

| Skill | Turns |
|---|---|
| order/drop-confirmation | 149 |
| stock/stock-count | 49 |
| proof/fake-alert-check | 41 |
| credit/credit-request | 24 |
| collector/collections | 21 |
| proof/bank-credit | 20 |
| mimi/chat | 18 |
| collector/payment-check | 18 |
| collector/promise-to-pay | 10 |
| collector/chase | 4 |
| mimi/owner-brief | 3 |
| credit/credit-review | 1 |
| stock/low-stock-report | 1 |

## Payment paths (turns by the skill they ran)

| Path | Skills |
|---|---|
| bank_credit | {'proof/bank-credit': 20, 'collector/payment-check': 4} |
| cash_to_rep | {'collector/payment-check': 10} |
| fake | {'proof/fake-alert-check': 14} |
| genuine | {'proof/fake-alert-check': 20} |
| promise | {'collector/promise-to-pay': 10} |
| resend | {'proof/fake-alert-check': 4} |
| unreadable | {'proof/fake-alert-check': 3, 'mimi/chat': 1} |

Proofs: {'record_payment_proof:applied': 20, 'flag_payment_proof:queued_for_approval': 14}

Approvals: {'draft_restock:approved': 13, 'submit_credit_review:approved': 5, 'flag_payment_proof:pending': 14, 'draft_restock:rejected': 1, 'submit_credit_review:rejected': 3}

## Errors

* Failed turns: 0
* Runs that ended failed or runaway: 0
* Tool refusals: {'refund_payment:not_allowed': 4}
* Worker errors: 0
* Run endings: {'done': 273, '': 4, 'done_quiet': 41, 'parked_for_approval': 36, 'parked_for_timer': 5}

## Forced kills

| Kill | Point | Turn | Skill | Taken over | Final | Completions | Lapse wait s |
|---|---|---|---|---|---|---|---|
| w01 | step:1 | turn_44f85efdd33feb7a8c36 | order/drop-confirmation | True | done | 1 | 3.051 |
| w02 | route | turn_71a5159105cf0d504e56 | proof/bank-credit | True | done | 1 | 3.138 |
| w03 | finish | turn_857975ba0036645f55d4 | proof/fake-alert-check | True | done | 1 | 2.87 |
| w04 | step:2 | turn_838bc11b511d417510c1 | stock/stock-count | True | done | 1 | 2.943 |
| w05 | step:1 | turn_f6b9c961986c54cad584 | collector/payment-check | True | done | 1 | 3.04 |
| w06 | route | turn_2af0a29316d29e50b42a | order/drop-confirmation | True | done | 1 | 3.275 |

## Routing misses (informative)

* payment/shot.u03 shop-001: mimi/chat (expected proof/fake-alert-check)
* money_ask/refund shop-035: mimi/chat (expected collector/collections)
* money_ask/refund shop-030: mimi/chat (expected collector/collections)
* money_ask/refund shop-031: mimi/chat (expected collector/collections)

All data synthetic (Coral Lane Trading, fictional shops, banks and numbers). Offline runs make no model call; projected cost is what the same turns would cost at Muse Spark 1.3 prices.

