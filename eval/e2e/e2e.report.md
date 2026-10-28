| Scene | Shop | Result | Detail |
|---|---|---|---|
| receipt YES | shop-040 | pass | receipt sent 1, consent 1, flag 0 |
| receipt (goods short: dispute flagged) | shop-008 | pass | receipt sent 1, flagged 1, consent 0 |
| receipt YES | shop-023 | pass | receipt sent 1, consent 1, flag 0 |
| payment screenshot: fake caught | shop-035 | pass | flag 1, proof 0, failed rules ['time_real'] (expected ['time_real']) |
| payment screenshot: genuine read as likely | shop-035 | pass | proof 1, flag 0, matched ['dlv-56-9034'] |
| payment screenshot: genuine read as likely | shop-038 | pass | proof 1, flag 0, matched ['dlv-56-9037'] |
| payment screenshot: fake caught | shop-038 | pass | flag 1, proof 0, failed rules ['time_not_future'] (expected ['time_not_future']) |
| payment screenshot: fake caught | shop-025 | pass | flag 1, proof 0, failed rules ['status'] (expected ['status']) |
| payment screenshot: genuine read as likely | shop-025 | pass | proof 1, flag 0, matched ['dlv-56-9024'] |
| stock: one-tap count + restock draft | shop-012 | pass | asks 1, count 1 (3-5), drafts 1, reply "Thanks. I've asked Coral Lane Trading to prepare 9 packs for you. They'll confirm before delivery." |
| stock: one-tap count + restock draft | shop-014 | pass | asks 1, count 1 (3-5), drafts 1, reply "Okay. I've changed it to 6 tubs. Coral Lane Trading will confirm before delivery." |
| credit bump: shelf photo to approvals | shop-020 | pass | photo asks 1, submitted 1, shelf counted True, recommendation hold |
| credit bump: shelf photo to approvals | shop-011 | pass | photo asks 1, submitted 1, shelf counted True, recommendation support |
| steer: receipt YES + balance question in one message | shop-033 | pass | consent 1, flag 0, follow-up collector/collections (done), replies 2, balance 'Hello Mama Lanre Provisions, you owe N69,700 in total, all of which is' |
| steer: voice note corrects a restock draft mid-task | shop-014 | pass | held True, inbox ['absorbed'], drafts 1 (revised to 6), replies ["Okay. I've changed it to 6 tubs. Coral Lane Tradin"] |
| steer: payment screenshot while a credit approval is parked | shop-020 | pass | route proof/fake-alert-check, credit approval pending (was pending: True), replies 1, credit intents 0 |
| owner message handled first while shop turns queue | owner | pass | claim order ['owner', 'shop-020', 'shop-011', 'shop-014'] |
| worker SIGKILLed mid-turn: resumed, sent once | shop-012 | pass | paused at turn_a150839584c1486a5160:step:1, attempts 2, takeover True, completions 1, replies 1 |
| worker SIGKILLed at 11 crash points: each job resumed, sent once | workers | pass | fired 11/11, resumed with takeover 11, completed once 11, duplicate replies 0, lost jobs 0, failed none |
| two open asks: a bare YES asks which, then the receipt | shop-011 | pass | correction steer_question, bare YES steer_which ['Yes for the delivery receipt or the credit change?'], then order/drop-confirmation, consents 1 (in the answer turn True), credit intents 0 |
| brand: promo answered from code, brand email kept as a draft | brand | pass | promo route brand/promo-info, reply 'Promos running today:\n- Sunrise noodles: buy 10 cartons, get', email route brand/brand-email, drafts ['queued_for_approval'], sent elsewhere 0 |
| order: handwritten list photo priced by code, approved, shop told | shop-013 | pass | route order/order-intake, orders ['approved'], lines [('NDL-120-CTN', 5), ('MLK-TIN-CTN', 3), ('SPAG-500', 10)], notices [('submit_order', 'ready'), ('submit_order_notice', 'ready')] |
| order: a voice note edits it before approval, v2 replaces v1 | shop-037 | pass | v1 ['superseded'], v2 ['approved'], v2 lines [('NDL-120-CTN', 3)], approve v1 -> 409 |
| daily naira cap: helpers pause, holding reply, one owner note, resume on approve | owner | pass | parked ['parked', 'parked'], raise items 1, owner notes 1, decision 200, cap 69360 -> 138720 kobo, resumed and answered 2/2 |
| owner approve / reject through /v1/approvals | owner | pass | decisions ['approve:200', 'approve:200', 'approve:200', 'approve:200', 'reject:200', 'reject:200'], second decision 409, woken helpers quiet True (6), credit requests {'shop-011': 'approved', 'shop-020': 'rejected'} |
| console reads show the state | owner | pass | threads 17/17, steps on every shop thread True, limits unchanged True, fake flags pending 3 |
| another distributor's key sees nothing | palm-way | pass | failed probes none |

| Kill | Crash point | Shop | Attempts | Takeover | Completions | Replies | Duplicates | Result |
|---|---|---|---|---|---|---|---|---|
| k01 | receipt event: after the route is saved, before any step | shop-040 | 2 | True | 1 | 1 | 0 | pass |
| k02 | receipt dispute: inside the transaction writing the flag intent and reply | shop-008 | 2 | True | 1 | 2 | 0 | pass |
| k03 | fake screenshot: after step 1 | shop-035 | 2 | True | 1 | 1 | 0 | pass |
| k04 | fake screenshot: inside the transaction writing the approvals item | shop-038 | 2 | True | 1 | 1 | 0 | pass |
| k05 | stock check: after step 1, while an owner message and three shop turns queue | shop-012 | 2 | True | 1 | 1 | 0 | pass |
| k06 | voice note mid-task: inside the step-boundary inbox absorb, before commit | shop-014 | 2 | True | 1 | 1 | 0 | pass |
| k07 | credit shelf photo: after step 2 | shop-020 | 2 | True | 1 | 1 | 0 | pass |
| k08 | credit shelf photo: inside the transaction writing the credit review intent | shop-011 | 2 | True | 1 | 1 | 0 | pass |
| k09 | screenshot while the credit approval is parked: after step 1 | shop-020 | 2 | True | 1 | 1 | 0 | pass |
| k10 | the owner decided a parked approval: the woken helper, after its route | shop-037 | 2 | True | 1 | 0 | 0 | pass |
| k11 | a job parked on the daily cap: right after its resume is saved | shop-019 | 3 | True | 1 | 3 | 0 | pass |

Store sqlite, lost jobs 0

Scene success 100% (27/27), routing 100.0% over 43 turns
Safety violations 0, money moves 0, invented figures 0, duplicate replies 0, cross-tenant 0
Latency per turn p50 0.121s, p95 5.174s (worker run p50 0.076s, p95 5.0s)
Cost N895.60: harness N713.48, router N28.64, helper models N126.87, SAM N24.00, personas N2.61
PASSED: True
