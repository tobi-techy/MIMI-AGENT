# COST.md: what one Mimi turn costs

Measured offline at ₦0: the end-to-end eval (`python -m mimi.api.e2e --no-write`,
RAI-272) replays one recorded live run of 10+ synthetic shops through the HTTP
API, the durable store and real worker processes. Every model call in that run
has its recorded token counts (input, cached input, output), so the cost of the
run is priced from real tokens without calling a model. Prices are in
`mimi/models/prices.py` (Meta Model API pricing, read 29 Oct 2026; ₦1,600 to
the dollar as a planning figure, `MIMI_USD_NGN` to change it).

## Per run and per turn (43 agent turns)

| | Agent cost | Per turn | Per 1,000 turns | Model calls |
|---|---|---|---|---|
| Before, as main counted it (cached input at the full rate) | ₦967.48 | ₦22.50 | about ₦22,500 | 117 |
| Before, cached input at Meta's cached rate | ₦769.13 | ₦17.89 | about ₦17,900 | 117 |
| **After (this branch, defaults)** | **₦705.93** | **₦16.42** | **about ₦16,400** | **111** |

"Agent cost" is every call Mimi makes: the harness loop, router and binding,
steer, helper models (readers), SAM and speech to text. It leaves out the
persona models that play the shops in the eval (₦2.61).

### Where the ₦261 drop comes from

| Change | Saving | Kind |
|---|---|---|
| Cached input priced at Meta's cached rate ($0.15 per 1M, not $1.25) | ₦198.35 | Accounting: the bill was already this; main overstated it |
| Plain-code paths for balance, promo and quiet-approval turns | ₦63.20 (6 fewer model calls) | Real spend cut |

So the honest spend cut is ₦769 to ₦706 (8%). The rest is counting what Meta
actually bills.

### After, by part (₦705.93)

| Part | ₦ |
|---|---|
| Harness loop (helpers) | 451.96 |
| Helper models (readers: screenshots, list photos, voice) | 126.87 |
| Steer (messages mid-task) | 74.46 |
| Router and pending-ask binding | 28.64 |
| SAM 3.1 (shelf photos) | 24.00 |

## What was cut or fixed

1. **One metering seam** (`mimi/models/metering.py`). Router, steer, binding,
   readers, SAM and speech to text are now booked to the daily naira cap and
   folded into the run budget. Before, the cap saw only harness spend (about 73%
   of the real cost) and the e2e report left out steer and router calls.
2. **Cached input at the cached rate**, prices in one place
   (`mimi/models/prices.py`). `MIMI_CACHED_RATE=0` restores the old full-rate
   accounting.
3. **Plain-code paths** (`mimi/harness/plain.py`, `MIMI_PLAIN_CODE`). Turns that
   are fully decided before any model looks at them run as code inside the same
   loop: same tools, tiers, Go policy, approvals, claims gate and idempotency.
   On by default: `balance`, `promo`, `approval_quiet`. Built but off until the
   e2e is re-recorded live: `delivery_receipt`, `stock_weekly`, `stock_tap`.
   Any surprise hands the step back to the real model.
4. **`prompt_cache_key`** per shared prefix (`mimi/models/cachekey.py`), sent in
   the request body only, so recordings replay unchanged. It raises the cache
   hit rate at volume; a replay cannot show that, so it is not in the figures
   above. `MIMI_PROMPT_CACHE_KEY=0` turns it off.
5. **Shared-prefix context layout** (`MIMI_CTX_SHARED_PREFIX=1`, off). Puts what
   every thread of a helper shares first, so a new thread re-pays only its own
   part. Off because it changes every recorded prompt; it needs a live
   re-record of the harness, context, credit, order and e2e evals.
6. The demo kill switch also refuses new and reset sessions (not a cost cut, it
   rides on this branch).

## How to measure it again

```bash
# after (defaults)
META_API_KEY= python -m mimi.api.e2e --no-write
# before the plain-code paths
META_API_KEY= MIMI_PLAIN_CODE=0 python -m mimi.api.e2e --no-write
# before, as main counted it: the "with cached input at the full rate" figure
```

The last lines of the output give `Cost`, `Agent ... over 43 turns` and the
full-rate figure. Measured 30 Oct 2026 on this branch; both runs passed.

## Limits

* Synthetic shops and one recorded run: real traffic has a different mix.
* The naira rate is a planning figure; Meta bills in dollars.
* The prompt cache key and shared prefix savings show up only on live traffic.
