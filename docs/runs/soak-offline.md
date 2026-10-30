# Simulated week: offline-soak run (seed 273, sqlite)

**PASS**. 10 week(s), 3632 turns in 453.5 s of wall time.

| Mark | Result | Pass mark |
|---|---|---|
| Turns complete | 100.0% ({'done': 3628, 'merged': 4}) | >= 98% |
| Invented figures sent | 0 | 0 |
| Claims-gate hits (held, not sent) | 91 | counted |
| Duplicate sends | 0 | 0 |
| Replayed message ids returning the first turn | 162/162 | all |
| Lost jobs | 0 | 0 |
| Lease takeovers with no planned kill | 26 | counted |
| Budget breaches (step, time, naira) | 0 | 0 |
| Daily cap breaches | 0 | 0 |
| Never-tier attempts (refused) | 42 | counted |
| Never-tier actions | 0 | 0 |
| Forced worker kills fired (each taken over, completed once) | 3 (3 ok) | |
| Routing (informative; offline router has no model) | 98.7% of 3417 | |
| Turn latency p50 / p95 / max | 0.438 / 1.173 / 3.195 s | |
| Cost | spent N0.00; projected at Muse Spark 1.3 prices N72,677.44 (N20.01 a turn) | |

## Skill mix (done turns)

| Skill | Turns |
|---|---|
| order/drop-confirmation | 1503 |
| stock/stock-count | 521 |
| proof/fake-alert-check | 410 |
| credit/credit-request | 238 |
| collector/collections | 210 |
| proof/bank-credit | 200 |
| collector/payment-check | 180 |
| mimi/chat | 174 |
| collector/promise-to-pay | 100 |
| collector/chase | 40 |
| mimi/owner-brief | 30 |
| credit/credit-review | 10 |
| stock/low-stock-report | 10 |
| mimi/escalate-owner | 2 |

## Payment paths (turns by the skill they ran)

| Path | Skills |
|---|---|
| bank_credit | {'proof/bank-credit': 200, 'collector/payment-check': 40} |
| cash_to_rep | {'collector/payment-check': 100} |
| fake | {'proof/fake-alert-check': 140} |
| genuine | {'proof/fake-alert-check': 200} |
| promise | {'collector/promise-to-pay': 100} |
| resend | {'proof/fake-alert-check': 40} |
| unreadable | {'proof/fake-alert-check': 30, 'mimi/chat': 9, 'mimi/escalate-owner': 1} |

Proofs: {'record_payment_proof:applied': 199, 'flag_payment_proof:queued_for_approval': 141}

Approvals: {'draft_restock:approved': 102, 'submit_credit_review:approved': 38, 'flag_payment_proof:pending': 141, 'draft_restock:rejected': 34, 'submit_credit_review:rejected': 41, 'raise_daily_allowance:approved': 4}

## Errors

* Failed turns: 0
* Runs that ended failed or runaway: 0
* Tool refusals: {'refund_payment:not_allowed': 42}
* Worker errors: 0
* Run endings: {'done': 2761, '': 37, 'done_quiet': 411, 'parked_for_approval': 356, 'parked_for_timer': 63}

## Forced kills

| Kill | Point | Turn | Skill | Taken over | Final | Completions | Lapse wait s |
|---|---|---|---|---|---|---|---|
| s001 | step:1 | turn_305ceee161d8c9220d8a | order/drop-confirmation | True | done | 1 | 3.261 |
| s002 | route | turn_98888d4b1163815eb78a | order/drop-confirmation | True | done | 1 | 3.311 |
| s003 | finish | turn_194ddb9f6a0d0ad96d0c | order/drop-confirmation | True | done | 1 | 3.064 |

## Routing misses (informative)

* payment/shot.u03 shop-001: mimi/chat (expected proof/fake-alert-check)
* money_ask/refund shop-035: mimi/chat (expected collector/collections)
* money_ask/refund shop-030: mimi/chat (expected collector/collections)
* money_ask/refund shop-031: mimi/chat (expected collector/collections)
* money_ask/refund shop-001: mimi/chat (expected collector/collections)
* payment/shot.u03 shop-001: mimi/escalate-owner (expected proof/fake-alert-check)
* money_ask/refund shop-023: mimi/chat (expected collector/collections)
* money_ask/refund shop-014: mimi/chat (expected collector/collections)
* money_ask/refund shop-027: mimi/chat (expected collector/collections)
* payment/shot.u03 shop-001: mimi/chat (expected proof/fake-alert-check)
* money_ask/refund shop-039: mimi/chat (expected collector/collections)
* credit/photo shop-019: order/drop-confirmation (expected credit/credit-request)
* money_ask/refund shop-022: order/drop-confirmation (expected collector/collections)
* money_ask/refund shop-035: mimi/chat (expected collector/collections)
* chat/hello shop-029: stock/stock-count (expected mimi/chat)
* money_ask/refund shop-008: mimi/chat (expected collector/collections)
* payment/shot.u03 shop-001: mimi/chat (expected proof/fake-alert-check)
* money_ask/refund shop-013: mimi/chat (expected collector/collections)
* money_ask/refund shop-028: mimi/chat (expected collector/collections)
* payment/shot.u03 shop-001: mimi/chat (expected proof/fake-alert-check)
* money_ask/refund shop-012: mimi/chat (expected collector/collections)
* money_ask/refund shop-029: mimi/chat (expected collector/collections)
* money_ask/refund shop-039: mimi/chat (expected collector/collections)
* money_ask/refund shop-027: mimi/chat (expected collector/collections)
* payment/shot.u03 shop-001: mimi/chat (expected proof/fake-alert-check)

## Soak loops

| Loop | Tenant | Waves | Seconds |
|---|---|---|---|
| 0 | coral-lane | 92 | 46.1 |
| 1 | coral-lane-s001 | 94 | 51.7 |
| 2 | coral-lane-s002 | 93 | 54.6 |
| 3 | coral-lane-s003 | 90 | 49.7 |
| 4 | coral-lane-s004 | 86 | 47.1 |
| 5 | coral-lane-s005 | 88 | 42.2 |
| 6 | coral-lane-s006 | 88 | 36.5 |
| 7 | coral-lane-s007 | 94 | 43.4 |
| 8 | coral-lane-s008 | 84 | 34.6 |
| 9 | coral-lane-s009 | 87 | 37.7 |

All data synthetic (Coral Lane Trading, fictional shops, banks and numbers). Offline runs make no model call; projected cost is what the same turns would cost at Muse Spark 1.3 prices.

