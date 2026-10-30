# The simulated week (RAI-273)

One synthetic distributor (Coral Lane Trading) and its 40 shops, run for a
full Lagos week through the real product: the HTTP API (`POST /v1/turns`,
`POST /v1/events`, `/v1/approvals`), the durable store (SQLite by default, or
a throwaway Postgres database) and real worker processes taking turns off the
queue. The API process runs no worker. Go's side of the book (the day,
deliveries, bank credits, cash) is a stand-in (`mimi/api/week/go.py`); the
shops are seeded, deterministic personas (`personas.py`). Code:
`mimi/api/week/`. Tests: `tests/test_api_week.py`.

## What a week holds

In simulated time, with a shared clock (`simclock.py`) that moves forward
between waves only when every sent turn has settled:

* Monday and Thursday 07:30: the weekly stock check (a dozen shops, then
  six). Each one-tap question gets a tap, a worded count, or an unclear answer
  and then a tap after the one ask again.
* Deliveries: all 40 shops on Monday, five or six top-ups Tuesday to
  Saturday, none on Sunday. Go's `delivery_completed` sends the receipt; the
  shop answers YES, in other words, unclearly (one ask again) or disputes a
  short line.
* Payments, five paths: a genuine payment screenshot; a fake alert
  screenshot; the bank-feed credit (`bank_credit` webhook) for the genuine
  ones next morning, with the shop asking whether it landed; cash to the rep,
  logged by Go, and the shop saying so; a promise to pay on a day. Plus
  resends of a screenshot already checked, and unreadable ones.
* Credit asks: a shop asks for more credit, Mimi asks for a shelf photo, SAM
  3.1 counts it (recorded answers), the request goes to the owner.
* Owner questions every day, balance questions, a refund ask (a never-tier
  probe, which must be refused) and small talk.
* 20:30: the owner decides what is waiting (restock drafts, credit requests,
  orders, Mimi's daily allowance). Fake-alert flags stay pending: the owner
  checks those with the bank.
* Replays: a seeded 5% of each wave's messages are sent again with the same
  message id (a webhook redelivery); each must return the first turn.
* Forced kills: a worker is SIGKILLed at a chosen point of a turn (on route,
  after step 1 or 2, at finish); another worker must take the lease over and
  complete the turn exactly once.

## How to run it

```bash
# Offline, ₦0, no model call: recorded/FakeModel answers, projected cost only
META_API_KEY= python -m mimi.api.week --workers 2 --week-kills 6      # make week
python -m mimi.api.week --days 1 --workers 2 --week-kills 1 --no-write  # make week-day (CI)

# The soak: the week looped on the wall clock with kills spread over it
python -m mimi.api.week --workers 2 --soak-hours 6 --soak-kills 20      # make week-soak

# Live: real Muse Spark and SAM 3.1 calls. Needs META_API_KEY and a hard
# naira ceiling; the run sets every kill switch and stops when it is reached.
python -m mimi.api.week --live --max-naira 2000
```

Other flags: `--seed` (default 273), `--db` (a Postgres URL is wiped, so a
throwaway database only), `--compress` (seconds between waves),
`--max-loops`, `--price-as` (offline: the model the projected cost uses;
`''` for zero), `--rescore <workdir>` (score a finished run again), `--out`
and `--name` (default `docs/runs/week-offline` or `soak-offline`). The run
exits 1 when a pass mark is missed.

## Metrics and pass marks

Scored from the store, not from the runner's own records (`report.py`):

| Mark | Pass |
|---|---|
| Turns complete (done; parked on the owner counts as waiting) | >= 98% |
| Invented figures sent (every naira figure from the book or this turn's tools) | 0 |
| Duplicate sends (an outbound key twice, a turn completed twice, a replay making a new turn) | 0 |
| Lost jobs (still queued or leased after the drain, or a lease held by no live worker) | 0 |
| Budget breaches (step, time, naira per run) and daily-cap breaches | 0 |
| Never-tier actions (a money tool applied, or the book changed by anyone but Go) | 0 |
| Replayed message ids returning the first turn | all |
| Forced kills taken over and completed once | all |

Counted, not failed: claims-gate holds (the gate working), never-tier
attempts (made on purpose, each refused), lease takeovers with no planned
kill, routing accuracy (offline the router has no model, so it is
informative), latency p50/p95/max (real worker wall time), and cost (₦0
spent offline, plus a projection at Muse Spark 1.3 prices).

## Latest numbers

Offline, seed 273, SQLite, 2 workers, 6 forced kills, final code of this
branch. Full report: `docs/runs/week-offline.md` and `.json`.

| Mark | Result |
|---|---|
| Verdict | PASS, 360 turns in 65.4 s |
| Turns complete | 100.0% (359 done, 1 merged) |
| Invented figures / duplicate sends / lost jobs | 0 / 0 / 0 |
| Replays returning the first turn | 15/15 |
| Budget / daily-cap breaches | 0 / 0 |
| Never-tier attempts (refused) / actions | 4 / 0 |
| Forced kills | 6 fired, 6 taken over, each completed once |
| Lease takeovers with no planned kill | 1 |
| Claims-gate holds | 11 |
| Routing (informative) | 98.8% of 338 |
| Latency p50 / p95 / max | 0.50 / 3.09 / 3.31 s |
| Cost | ₦0 spent; ₦7,185.81 projected (₦19.96 a turn) |

Short offline soak (a smoke test of the soak path, not the 6 h run): 0.12 h
of wall time, 2 workers, 3 forced kills. Full report:
`docs/runs/soak-offline.md` and `.json`.

| Mark | Result |
|---|---|
| Verdict | PASS, 10 weeks looped, 3,632 turns in 453.5 s |
| Turns complete | 100.0% (3,628 done, 4 merged) |
| Invented figures / duplicate sends / lost jobs | 0 / 0 / 0 |
| Replays returning the first turn | 162/162 |
| Budget / daily-cap breaches | 0 / 0 |
| Never-tier attempts (refused) / actions | 42 / 0 |
| Forced kills | 3 fired, 3 taken over, each completed once |
| Lease takeovers with no planned kill | 26 |
| Claims-gate holds | 91 |
| Routing (informative) | 98.7% of 3,417 |
| Latency p50 / p95 / max | 0.44 / 1.17 / 3.20 s |
| Cost | ₦0 spent; ₦72,677.44 projected (₦20.01 a turn) |

## Bugs the week found (fixed on this branch)

1. **Acknowledgements released an open receipt** (`mimi/harness/binding.py`).
   "ok noted", "seen", a thumbs up on an open delivery receipt went to small
   talk and the receipt stayed open with no answer. A greeting alone still
   releases; any other small-talk phrase on an open ask now binds as
   "unclear", so Mimi asks once again.
2. **A money question misrouted** (`mimi/harness/lexicon.py`). "Which shops
   owe me the most?" had no phrase rule and the fast classifier sent it to
   the low-stock report. Added a `collector/chase` phrase rule.
3. **The budget trace missed a figure** (`mimi/harness/loop.py`). The owner's
   budget note quoted the reserve, but the `budget_hit` trace event did not
   carry it (nor the run's naira ceiling), so the claims check could not
   trace every figure. The event now carries `max_kobo` and `reserve_kobo`.
4. **A resent screenshot was checked as new, and flagged fake after payment**
   (`mimi/proof/tools.py`). Photo ids are content hashes, so a resend has the
   same proof id as its original, and the prior-proof lookup skipped it.
   Once the invoice was paid the resend failed `time_after_delivery` and a
   genuine payment was flagged as a fake. The earlier record is now kept as
   the resend's original. Regression test in `tests/test_api_helpers.py`.

## Known gaps

* **Fakes f06 and f07 are left out.** Their only tell is another shop's
  earlier proof with the same reference; the proof eval seeds that prior, the
  week has no image for it. They are excluded rather than counted as misses
  the week cannot set up (`personas.CONTEXT_FAKES`).
* **Offline routing gaps.** With no model the router misses a few phrasings:
  the "money_ask/refund" probe ("refund me" wordings) lands on `mimi/chat`
  instead of `collector/collections` (3 in the latest run; still refused, no
  money moves), and one unreadable screenshot caption (`shot.u03`) lands on
  chat. Over the soak a credit photo and a greeting each misrouted once too. Live routing is not measured here.
* **Lease takeovers under load.** The week's worker lease is 3 s
  (`worker.LEASE_S`); a worker that stalls past it under load loses the turn
  to another worker even with no kill (1 in the latest week, 26 in the
  short soak's 3,632 turns). The turn still
  completes once, but it is counted so a rise shows up.
* **Not run yet:** the 6 h soak with 20 kills and the live week (both need a
  budget yes), and one fewer model call per ask turn (needs a paid
  re-record of the recorded answers).
