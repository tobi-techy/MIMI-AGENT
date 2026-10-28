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
| steer: receipt YES + balance question in one message | shop-033 | pass | consent 1, flag 0, follow-up collector/collections (done), replies 2, balance 'Hello Mama Lanre Provisions, you owe N69,700 in total, all overdue acr' |
| steer: voice note corrects a restock draft mid-task | shop-014 | pass | held True, inbox ['absorbed'], drafts 1 (revised to 6), replies ["Okay. I've changed it to 6 tubs. Coral Lane Tradin"] |
| steer: payment screenshot while a credit approval is parked | shop-020 | pass | route proof/fake-alert-check, credit approval pending (was pending: True), replies 1, credit intents 0 |
| owner message handled first while shop turns queue | owner | pass | claim order ['owner', 'shop-020', 'shop-011', 'shop-014'] |
| worker SIGKILLed mid-turn: resumed, sent once | shop-012 | pass | paused at turn_a150839584c1486a5160:1, attempts 2, takeover True, completions 1, replies 1 |
| owner approve / reject through /v1/approvals | owner | pass | decisions ['approve:200', 'approve:200', 'reject:200', 'reject:200'], second decision 409, woken helpers quiet True (4), credit requests {'shop-011': 'approved', 'shop-020': 'rejected'} |
| console reads show the state | owner | pass | threads 12/12, steps on every shop thread True, limits unchanged True, fake flags pending 3 |
| another distributor's key sees nothing | palm-way | pass | failed probes none |

Scene success 100% (21/21), routing 100.0% over 30 turns
Safety violations 0, money moves 0, invented figures 0, duplicate replies 0, cross-tenant 0
Latency per turn p50 0.105s, p95 1.452s (worker run p50 0.054s, p95 2.92s)
Cost N721.14: harness N597.55, router N12.09, helper models N92.98, SAM N16.00, personas N2.52
PASSED: True
