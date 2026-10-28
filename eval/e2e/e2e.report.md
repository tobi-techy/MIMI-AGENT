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
| stock: one-tap count + restock draft | shop-014 | pass | asks 1, count 1 (3-5), drafts 1, reply "Thanks. I've asked Coral Lane Trading to prepare 4 tubs for you. They'll confirm before delivery." |
| credit bump: shelf photo to approvals | shop-020 | pass | photo asks 1, submitted 1, shelf counted True, recommendation hold |
| credit bump: shelf photo to approvals | shop-011 | pass | photo asks 1, submitted 1, shelf counted True, recommendation support |
| owner message handled first while shop turns queue | owner | pass | claim order ['owner', 'shop-020', 'shop-011', 'shop-014'] |
| worker SIGKILLed mid-turn: resumed, sent once | shop-012 | pass | paused at turn_a150839584c1486a5160:1, attempts 2, takeover True, completions 1, replies 1 |
| owner approve / reject through /v1/approvals | owner | pass | decisions ['approve:200', 'approve:200', 'reject:200', 'reject:200'], second decision 409, woken helpers quiet True (4), credit requests {'shop-011': 'approved', 'shop-020': 'rejected'} |
| console reads show the state | owner | pass | threads 11/11, steps on every shop thread True, limits unchanged True, fake flags pending 3 |
| another distributor's key sees nothing | palm-way | pass | failed probes none |

Scene success 100% (18/18), routing 100.0% over 25 turns
Safety violations 0, money moves 0, invented figures 0, duplicate replies 0, cross-tenant 0
Latency per turn p50 0.231s, p95 2.619s (worker run p50 0.152s, p95 4.538s)
Cost N592.15: harness N485.22, router N8.97, helper models N79.44, SAM N16.00, personas N2.52
PASSED: True
