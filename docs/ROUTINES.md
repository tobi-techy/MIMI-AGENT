# Scheduled routines (4 Nov 2026)

Nothing used to run unless someone asked. A distributor's book ages by itself,
so the owner should hear about it without opening a console. This is the
mechanism that closes that gap, and nothing more: **Mimi proposes, code checks
the owner approves**. A routine reads the book and files what it found; no
routine writes a number, moves money or calls a model.

Code: `mimi/api/routines.py`. Tables: `routine_state` and `routine_runs`
(alembic `0018`, on `0017`). Tests: `tests/test_routines.py`.

## The rail

| Routine | Schedule | What it files |
|---|---|---|
| `morning_brief` ("Morning briefing") | Every day at 8:00 AM | The shop most overdue by name and ₦, on the owner's thread |
| `aging_scan` ("Aging scan") | Every day at 8:30 AM | Shops whose oldest money has passed 60 days, and transfer alerts that never matched a payment |

Both are code over the same book the reads use (`mimi.owed`, `SqlGo`,
`demo_wiring.brief_text`). Times and days are Africa/Lagos through
`mimi.tz`; the stamps come from the one clock, `mimi.clock.wall()`.

The schedule is a `Schedule(hour, minute)` with `words()` written for a
person ("Every day at 8:00 AM"), never a cron string. `next_after(now)` is
the first time the schedule comes round strictly after now.

## Once per Lagos day, per routine

`routine_runs` is the claim, and its primary key is
`(tenant_id, routine, day)` with `day` the Lagos calendar day. A tick claims
the day with one `INSERT ... ON CONFLICT DO NOTHING`
(`store.util.insert_ignore`): exactly one caller, in one process, anywhere on
the network, can write that row. Every other tick, in this process or another
API replica, loses the insert and does nothing. The claim is never released,
including after a crash, so the routine's message can never go out twice on a
day.

The message is also idempotent on its own: `out:routine:{id}:{day}`. That is
belt and braces, not the mechanism.

The owner's `POST /v1/routines/{id}/run` takes the same claim, so it fires at
most once too. Running a routine by hand before its scheduled time uses that
day: the 8:00 AM tick later sees the claim and does nothing. For a demo that
is the point, but run it before the Lagos day whose scheduled run matters.

A run that throws after the claim is recorded `failed` in `routine_runs` and
in `routine_state`; the same day is not retried, and the next tick carries on
with the other routines. The next day runs normally.

## Late starts, missed windows

A tick at 9:00 still sends the 8:00 brief: a window of `CATCH_UP` (4 hours)
after the scheduled time lets a restart recover. A window is cut at Lagos
midnight, so a run just before midnight never claims the next day, and the
day key never borrows the current time. The window is per routine, so at
12:15 the aging scan (8:30, open until 12:30) still catches up while the
brief (8:00, closed at 12:00) does not; that is one rule, not two.

A routine with a run history that misses its window is recorded `missed`
once, with words that state only what the code knows: "It did not run today.
Its 8:00 AM time has passed." It does not claim the API was down, because it
cannot know that; a deploy, a restart, a young process and a new tenant look
the same from here.

A routine that has already run today is never a miss. The gate is
`last_run_day == day`, not "has it ever run": once a run is recorded, a tick
after its window says `already_claimed` and leaves the finding and the
`last_run_at` stamp alone. Otherwise a run at 12:35 would be overwritten by
its own window closing at 12:36, which is exactly what the rail did before
this fix.

A routine with no history at all is not missed. A book opened at noon did not
exist at 8:00, so its rail stays at "not run yet" (empty `last_finding`) and
the day is not written as a miss. It can still catch up if it is inside the
window, which is why a fresh tenant at 12:09 gets the 8:30 aging scan's real
finding and no morning brief.

## The switch

`routine_state` holds one row per routine and tenant:

* the routine's own `paused` and `pause_reason`;
* the last run (`last_run_day`, `last_run_at`) and what it found
  (`last_finding`), or the last skip/miss/failure, with its status;
* `last_finding_shop_id`, set only when the last finding is about exactly one
  shop. The console links that shop; several shops or the whole book leave it
  empty and the sentence renders as plain text. A skip, miss or failure clears
  it, so a link never outlives the finding it belonged to;
* one row with `routine = "*"`, the kill switch: paused, it stops every
  routine at once.

It is a row because at 3am the owner needs a switch that survives a restart
and needs no deploy. `POST /v1/routines/{id}/pause` and `/resume` set it;
`id` of `all` (or `*`) is the kill switch. The row can also be written with
SQL if the console is unreachable: `paused = 1` on the `*` row for the
tenant.

A paused routine still records that it skipped the day, and why: the next
tick at or after its scheduled time sets `last_status = "skipped"`,
`last_skip_day`, and `last_finding = "It did not run today: it is paused."`.
The reason itself is `paused_reason`, a sentence ("Checking the early run.").
Resuming inside the window still runs that day, because a pause is not a
claim.

## Findings: a proposal or a note, never an action

A routine returns a `Finding` with the thread's words, a one-line summary, and
the shops that summary is about (none, one, or several); the writer
(`file_finding`) only ever writes one message on the owner's thread. It does
not call a write tool, change a receivable, a payment, an approval or a stock
row. `test_a_routine_has_no_model_and_never_writes_the_book` digests the book
and the approvals before and after both routines and compares: no change, no
spend row.

The morning brief's line names the single most overdue shop, with the book's
own integers turned into naira by code: "Mama Sola Provisions owes ₦3,497,300.
Of that, ₦2,387,250 is overdue, and the oldest invoice is 45 days old." It
carries that shop's id, so the console can link it without reading the
sentence. The aging scan's line names a shop only when the whole finding is
about one shop; several shops or a list of alerts leave the id empty.

A routine that would have to change a number must stop and propose through
the product's existing approval path. Neither of these two needs to: the
brief and the scan report. The moment a routine needs to act, its finding
becomes an approval (the owner approves; nothing moves money).

## What it costs

Nothing per run. The scheduler is constructed with an engine and nothing
else: no `Agent`, no model port, no HTTP client. Each routine declares
`model_calls` (0 here), so a future routine that would spend naira has to say
so and a test holds the line; the cap is 0 because the cost is 0.
`test_a_routine_has_no_model...` also checks the tenant's `spend` table is
empty after a full day's runs. The only database work is a handful of reads,
one claim insert, and at most one message insert per routine.

## API

```
GET /v1/routines          owner key
{ "routines": [ { "id", "name", "schedule", "paused", "paused_reason"?,
                  "last_run_at", "next_run_at", "last_finding",
                  "last_finding_shop_id"? } ],
  "as_of": "2026-11-04T08:31:00+01:00" }

POST /v1/routines/{id}/run     owner key
  the routine's row, as GET /v1/routines reads it after the run
  (409 with a sentence if it is paused or already ran today)

POST /v1/routines/{id}/pause   owner key
POST /v1/routines/{id}/resume  owner key
{ "id": "morning_brief", "name": "Morning briefing", "paused": true }
```

`name`, `schedule` and `last_finding` are sentences written by code and
rendered as written. `last_run_at` and `next_run_at` are ISO 8601 with an
explicit zone, and `last_run_at` is empty until the routine really has run;
`last_finding` is empty then too, because the console words "Not run yet"
from the empty timestamp and a sentence would say it twice. A skip, miss or
failure is something that happened, so those sentences stay. `paused_reason`
is present only when paused. `last_finding_shop_id` is
present only when the last finding names one shop; it is never guessed from
the sentence. Resuming one routine under the kill switch answers
`paused: true`: the kill switch is the truth.

`/run` is the owner's button for a demo or a check. It goes through the tick's
own `_run_one`: the same finding, writer, message and recording, with only the
clock gate skipped because the owner said now. It answers with the row after
the run, so the rail is never a separate implementation. A paused routine
answers 409 "It is paused, so it did not run. The reason: ..."; a routine that
already ran today answers 409 "It has already run today. It runs again
tomorrow at 8:00 AM." Neither sends anything.

## Wiring and settings

* `MIMI_ROUTINES=1` (the Dockerfile) starts one daemon thread in the API
  process. Off (CI, `make check`, or `MIMI_ROUTINES=0` in a container) no
  thread starts, so a test's seeded book is never written behind its back;
  the routes and tables still work.
* It is one thread per process. `MIMI_WORKERS` adds turn-worker threads
  inside the same process, not processes, so they share that one thread. If
  a platform ever runs several processes, each has its own scheduler; the
  day's claim means only one fires, but the rest still tick and do nothing,
  so run one process (or set `MIMI_ROUTINES=0` on the extras).
* `MIMI_DEMO_KILL=1` starts no scheduler at all: the demo's kill switch stops
  routines with turns and sessions. The per-routine pause and the `*` row are
  the no-restart switches.
* A tick opens its own short-lived connection and closes it before waiting
  again: an idle tick loop holds nothing on the pool, so it cannot starve
  demo traffic between ticks.
* `MIMI_ROUTINES_TICK_S` changes how often the thread looks (default 30 s,
  minimum 1 s). It is a poll, not a timer per routine: no infinite loop can
  take the API down, and every exception in a tick is caught, logged and
  recorded.
* There is no new container and no cron. A second API replica is safe by the
  claim; it is not required.

## What was deliberately not built

* A dashboard or a push channel: findings land on the owner's thread, where
  the product already puts anything a person must see.
* A routine that acts. The rail can file an approval, but neither of the
  first two has a number to change, so both only report.
* A per-routine history of every run in the API: the read shows the last
  run/finding; `routine_runs` keeps one row per routine per day for audit.
* Retrying a failed run the same day: the claim is once a day, so a crash
  cannot double-send. The failure is visible as a sentence and the next day
  runs normally.
* A manual run that bypasses the day's claim: the owner's button exists so a
  demo does not wait for 8:00 AM, not so a finding can go out twice.
