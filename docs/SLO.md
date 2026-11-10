# SLO.md: the five numbers the pilot is judged on

This is the page a responder opens at 02:00. Every service level objective
here has a number, a metric, a first action and an owner. Nothing on this page
is a wish: each metric is emitted today by `mimi.observe.metrics` and each
alert exists in `ops/prometheus/mimi.rules.yml`. `tests/test_slo.py` fails if
a threshold here loses its metric, or an alert here names a series the code
does not emit.

Scale the numbers assume: tens of distributors, up to 10,000 shops, one turn
per shop message. Pilot, not a public launch.

Read `[PROVISIONAL]` as "a real number nobody has measured under load yet".
The list at the bottom says which ones, and `docs/SLO.md` is the only place
they are written down, so a placeholder cannot be quoted later as a fact.

## The five

| # | Objective | Metric (Prometheus) | Threshold | Owner |
|---|---|---|---|---|
| SLO-1 | Turns finish without failing | `mimi_turn_failure_rate{window="1h"}` | under 1% over 1h `[PROVISIONAL]` | on-call engineer |
| SLO-2 | A code-only turn answers fast | `mimi_turn_latency_ms{quantile="p95"}` | under 1,000 ms `[PROVISIONAL on the live box]` | on-call engineer |
| SLO-2b | A model turn answers before the shop gives up | `mimi_turn_latency_ms{quantile="p95"}` | under 20,000 ms `[PROVISIONAL]` | on-call engineer |
| SLO-3 | Queued work starts soon | `mimi_queue_oldest_wait_seconds` | under 60 s | on-call engineer |
| SLO-4 | A waiting approval is decided | `mimi_approvals_oldest_wait_seconds` | under 4 h in working hours `[PROVISIONAL]` | founder (operations) |
| SLO-5 | No distributor runs past its day's model allowance | `mimi_spend_used_fraction` | at or under 1.0, every tenant, every day | founder (operations) |

---

### SLO-1 Turn success rate

- **Metric:** `mimi_turn_failure_rate{window="1h"}`, with
  `mimi_turns_finished{window="1h"}` beside it so a rate never reads from a
  quiet hour as if it were healthy. The day's rate and a per-helper breakdown
  (`mimi_helper_failure_rate{helper=...}`) are on the same page.
- **Threshold:** under 1% of finished turns over an hour `[PROVISIONAL]`. The
  measured laptop figure is 0 failures in 420 turns (`docs/SLO.md#measured`),
  which is a smoke test, not a rate.
- **Alert:** `MimiTurnFailureRate`, fires when the 1h rate is above 2% for 15
  minutes. Two percent, not one, so a single poisoned thread of thirty turns
  does not page anyone.
- **First action:** open `/metrics`, read `mimi_helper_failure_rate` and find
  the helper that is failing. Then read one turn's status in the `turns`
  table for that helper (`failed_provider`, `failed_tool`, `stopped_runaway`,
  `poison`). Provider errors point at the model vendor's status page; tool
  errors point at Go or the connector. If the helper is a connector, run the
  connector's own sync once by hand before touching the queue.
- **Owner:** on-call engineer. Escalation: the founder when a failure keeps
  money out of an owner's hands (a reply not sent, an approval not created).

### SLO-2 Turn latency

- **Metric:** `mimi_turn_latency_ms{quantile="p50"|"p95"}` over the last hour,
  computed from `turns.created_ts` to `finished_at`.
- **Threshold:** the code-only path answers in under 1,000 ms at p95, and any
  turn that needs the model in under 20,000 ms at p95 `[PROVISIONAL]`.
  Measured on this laptop (SQLite, one worker, no network): p50 33 ms, p95
  45 ms, p99 66 ms for a code-only balance turn; p50 35 ms, p95 81 ms with one
  scripted model call that never touches the network. The live p95 is the
  provider's round trip plus that 45 ms, so the 20,000 ms number is a
  placeholder until the load run reports one `[PROVISIONAL]`.
- **Alert:** `MimiTurnLatency`, fires on p95 above 30,000 ms for 10 minutes.
- **First action:** check `mimi_turns_per_minute` first. If traffic is normal
  and latency is up, it is the model provider: read the gateway's
  `model_fallback` events in `thread_events` for the hour and confirm whether
  the fallback model took over. If traffic is 10× normal, it is the queue:
  look at `mimi_queue_depth` and add a worker before touching a timeout.
- **Owner:** on-call engineer.

### SLO-3 Queue age

- **Metric:** `mimi_queue_oldest_wait_seconds`, the age of the oldest turn in
  `queued` or `leased`, and `mimi_queue_depth` / `mimi_queue_leased`.
- **Threshold:** under 60 s. A shop's message is a person waiting; a minute is
  the point where the reply reads as broken. Measured ceiling: one worker
  moves 27.6 code-only turns a second on SQLite, so 60 s of age means the
  worker is down or stuck, not busy.
- **Alert:** `MimiQueueAge`, warning above 300 s for 5 minutes; `MimiQueueStuck`
  critical above 900 s for 5 minutes. Two stages because a restart is cheap
  and deserves a warning, while fifteen minutes of silence deserves a page.
- **First action:** check the worker is alive (`mimi_queue_leased` greater
  than zero with an unchanging `mimi_queue_oldest_wait_seconds` means a
  worker holds a lease and is stuck; zero leased with depth rising means no
  worker is polling). Then check the oldest turn's `lease_expires_ts`: an
  expired lease with no owner is a crash, and the queue's own takeover will
  pick it up at the next poll. Never delete a queued turn: `docs/LOOP-SAFETY.md`
  is the contract for stuck work.
- **Owner:** on-call engineer.

### SLO-4 Approval to decision

- **Metric:** `mimi_approvals_waiting` and
  `mimi_approvals_oldest_wait_seconds` (pending approvals, from
  `approvals.created_ts`).
- **Threshold:** the oldest pending approval is decided within 4 hours during
  working hours `[PROVISIONAL]`, because nothing a helper drafts reaches a
  shop until the owner approves it: a waiting approval is a stalled reply, not
  a queue of paperwork.
- **Alert:** `MimiApprovalsWaiting`, warning when the oldest pending approval
  is older than 4 hours (14,400 s) for 15 minutes; critical at 24 hours. The
  alert does not page at night: it is the owner's backlog, not a fault, so it
  carries `severity: ticket` and only becomes `page` past 24 hours.
- **First action:** open the owner's approvals list and read the oldest item's
  `preview`. If it is a `raise_daily_allowance`, approve or reject it today:
  parked work resumes on a yes and stays parked on a no (nothing is dropped
  either way). If it is a shop-facing draft, the shop is waiting; approve or
  reject it, do not leave it pending.
- **Owner:** founder (operations).

### SLO-5 Spend per tenant per day

- **Metric:** `mimi_spend_used_fraction{tenant=...}`, with
  `mimi_spend_turns_used{tenant=...}` and `mimi_spend_turns_cap{tenant=...}`
  beside it and `mimi_spend_today_kobo`, `mimi_spend_cap_kobo`,
  `mimi_spend_active_shops` and `mimi_spend_used_fraction` behind them.
- **Threshold:** at or under 1.0 for every tenant, every day, and never a day
  where the allowance was exhausted without the owner being asked. The cap is
  N250 a day per active shop (`mimi.observe.allowance`), fixed at Lagos
  midnight. It is enforced in the runtime, not here: `mimi.observe.spend`
  refuses the paid call before it is made, and `tests/test_observe_spend.py`
  is the proof. This SLO is about the enforcement working, not about the bill
  being small.
- **Alert:** `MimiSpendNearlySpent`, warning when a tenant's used turns reach
  80% of its cap; `MimiSpendCapped`, ticket when used turns reach the cap. The
  alert names the tenant and reads in turns, because that is the sentence the
  founder can act on: "tenant X used 118 of 121 model turns today". The kobo
  figures are in the rule's description and on the page.
- **First action:** read the tenant's `spend` row and the day's `cap_hit`
  events. If the cap was hit and the owner has not been asked, that is a bug:
  check `turns.status = 'parked_budget'` has a matching approval. If the owner
  was asked and has not decided, that is SLO-4, not a spend fault. Raise the
  allowance only through the owner's approval; never by editing the row.
- **Owner:** founder (operations).

## The allowance: N250 per active shop a day

The owner decided this on 30 Oct 2026. It is implemented once, in
`mimi/observe/allowance.py`, and read by both the gate
(`SqlGo.daily_spend`) and the report (`mimi.observe.metrics`), so the number
shown and the number enforced cannot disagree.

```
cap_today  = 250_00 kobo  ×  max(1, active_shops)      # floored at one shop
cap_today  =  max(cap_today, raise_approved_today)     # an owner raise, that day only
```

**An active shop** is a shop with any recorded activity in the 30 days
*before* today: a message, a turn, an order, a payment or a return. A message
counts through the turn it starts, so there is one source for it and nothing
double counted. A shop that is only a row in `shops` buys nothing: a
distributor with 400 dormant shops is not handed N100,000 a day.

**The window ends at today's Lagos midnight**, which is what makes the number
stable and what makes today's own activity irrelevant: a shop added, woken or
delivered-to mid-day does not raise today's cap. It raises tomorrow's, at
midnight, when the window moves. That is deliberate and it is worth saying
plainly, because a founder who adds shops and sees no budget change will
otherwise think it is broken.

**Money is the limit; turns are the unit.** The cap is enforced in kobo,
because a vision turn reading a screenshot costs many times a text turn and a
turn-count limit would overspend a money limit. When the two disagree, money
wins. What the owner reads is turns, derived from the measured N16.42 average
in `COST.md` (`mimi.observe.spend.turns_for`), with the naira behind it.

The founder's own cases, worked through:

| Active shops | Cap a day | Model turns (at N16.42) |
|---|---|---|
| 1 (or none: the floor) | N250 | 15 |
| 8 | N2,000 | 121 |
| 40 | N10,000 | 609 |

Those turn counts are what a distributor gets if *every* turn needs the
model. In practice the rule and plain-code layers answer a large share
(balance, promo, quiet approvals, delivery receipts, stock taps) at N0, so a
live distributor's paid-turn count is lower than M: at half of turns served
by code, 121 paid turns is about 242 shop messages. The multiplier stays at
N250 a shop; the arithmetic is here rather than an adjustment to it.

**An approved raise wins for its own day only.** The `raise_daily_allowance`
approval carries the day; the day after, the shop count sets the cap again.
The tenant column `daily_cap_kobo` stays as the audit trail of the last raise
and is no longer what the gate reads. The emergency stop for a tenant is the
kill switch, not a zero budget.

## What happens when a distributor is out of allowance

The degradation is defined, not improvised (`mimi.observe.spend`):

1. **The paid call is refused before it is made.** The metering seam asks the
   day's allowance, so every worker sees the same limit and no extra tokens
   are bought while a bill is being reconciled.
2. **The cheap, certain path keeps working.** Rules, book reads and the
   plain-code replies (balance, promo, quiet approvals, delivery receipts,
   stock taps) need no model and are not paused. A shop that asks what it owes
   still gets the right figure. `tests/test_observe_spend.py` fails if that
   stops being true.
3. **The shop gets one plain sentence, written by code,** and nothing is lost:
   the turn parks as `parked_budget` and resumes when the owner raises the
   allowance.
4. **The owner is asked once a day** with the figures on an approval card,
   turns first: "Mimi has used 118 of today's 121 model turns (N1,938 of
   N2,000), so helpers are paused until tomorrow. Raise today's allowance?"
   Approving re-queues every parked turn of that distributor.
5. **The founder can see it without reading a log.** `cap_hit` is a
   `thread_events` row with the figures, a `log.warning` line naming the
   tenant, and `mimi_spend_used_fraction` reaching 1 is the alert. A tenant at
   its cap is an item with a tenant, an amount and what was refused, not a 429
   nobody sees.
6. **The default is a number.** A caller that forgot to pass a cap, or passed
   a negative one, gets the one-shop floor. "Unlimited" is not a state this
   code can reach.

## Measured, and what that implies

Measured on 30 Oct 2026, one laptop, SQLite, one worker process, 300
code-only turns and 120 turns with a scripted model call (no network):

| Path | p50 | p95 | p99 | Throughput |
|---|---|---|---|---|
| Code-only turn (balance) | 33 ms | 45 ms | 66 ms | 27.6 turns/s |
| One model call (scripted, no network) | 35 ms | 81 ms | 156 ms | 23.3 turns/s |

At 10,000 shops with one turn a day that is 10,000 turns a day, about 0.1
turns a second: more than two hundred times under one worker's measured
ceiling. The pilot's limit will be the model provider's latency, not this
process.

Spend, from `COST.md` (priced from recorded tokens, not estimated): N16.42 a
model turn at the current defaults. That is the number behind every "N of M
model turns" on this page, and it is why the allowance is a shop count times
N250 rather than a single flat figure: `docs/SLO.md#the-allowance-n250-per-active-shop-a-day`
works the founder's cases (8 shops, 40 shops) through to turns.

## Provisional numbers

Every number marked `[PROVISIONAL]` is waiting for the load run the release
agent reports, measured on the pilot deployment, not a laptop:

- **SLO-1** the 1% failure rate: 0 failures in 420 local turns is a smoke test,
  not a rate.
- **SLO-2** the 20,000 ms model-path p95 and the 1,000 ms code-path p95 on the
  live box: the laptop numbers above are the floor, not the target.
- **SLO-2b** the model-path half of SLO-2 above (20,000 ms), listed separately
  because it is the one the provider sets, not this process.
- **SLO-4** the 4-hour approval budget: a working-hours guess, to be set with
  the founder against real approval traffic.

When those measurements land, put them in with the environment named (local
Postgres is not Neon and neither is the pilot box) and keep the tag until the
number comes from the environment we actually run. Change the number here and
in `ops/prometheus/mimi.rules.yml` in one commit: `tests/test_slo.py` keeps
the two files honest about each other.

The allowance in SLO-5 is **not** provisional: N250 per active shop a day is
the founder's decision, and the only derived number in it, N16.42 a turn,
comes from `COST.md`'s recorded tokens. If that average moves materially,
`mimi.observe.spend.AVG_TURN_KOBO` moves with it and the turn column in the
allowance table is recomputed.

## Where the alerts live

`ops/prometheus/mimi.rules.yml` is the alert set and
`ops/prometheus/README.md` is the scrape configuration, including the bearer
token the endpoint requires. There is no vendor in this path: the endpoint is
Prometheus text, the rules are Prometheus rules, and any scraper that speaks
that text can read them. Grafana, if the team wants it later, reads the same
series.

## Not covered yet, on purpose

- **No per-shop or per-rep spend.** The bill is per distributor because that
  is who pays; splitting it further needs the owner to say what a shop's
  budget means.
- **No alert routing.** The rules carry severity and a runbook link; which
  pager receives them is deployment configuration, not code
  (`ops/prometheus/README.md` says what each severity expects).
- **No paging on trace leakage.** `mimi.observe.sinks` drops a leaking payload
  and logs it (`trace dropped: ...`), and the log is the alert source until the
  log pipeline is in place: a `trace dropped` line in production is a bug, not
  noise.
- **No database migration for the correlation column.** Approval plus turn id
  is the join today (`mimi/observe/correlation.py`); the column and its switch
  are in the later DB revision and the join is pinned by
  `tests/test_observe_trace.py`.
