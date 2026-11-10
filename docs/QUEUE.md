# The turn queue

Mimi's durable work queue is the `turns` table (`mimi/store/schema.py`), driven
by `mimi/store/queue.py`. A turn is one unit of work: read the owner's or
shop's message, run the helper, write the reply and any approved intent. The
queue is what makes "it is in the database, so it will run" true across a
worker restart, a deploy, a lost lease or a code bug that poisons one job.

There is no in-memory queue and no second jobs table. `runtime_jobs` is the
harness runner's own state machine and is not the agent queue.

## Claim protocol (Postgres)

`TurnQueue.claim(worker_id)` reads up to `CANDIDATE_LIMIT` (500) runnable rows:
`state = 'queued'` with no lease or an expired one, or `state = 'leased'` with
an expired lease. A turn whose thread is already busy is deduped to the oldest
runnable turn on that thread, then sorted lapsed-lease first, then priority,
then age.

For each candidate, in one transaction (`_claim_txn`):

1. `SELECT ... FROM turns WHERE turn_id = :id FOR UPDATE SKIP LOCKED`. Another
   worker's row lock is skipped, not waited for.
2. The thread row is locked the same way. A worker busy with the thread makes
   this claim skip to the next candidate instead of stalling behind it.
3. A poison check (`attempts >= MAX_CLAIMS`) parks the turn instead of leasing
   it (see dead letter below).
4. **Per-tenant cap.** `pg_advisory_xact_lock(hashtext(tenant_id))` serialises
   this tenant's claims across processes, then a count of that tenant's live
   leases (`state = 'leased' AND lease_expires_ts >= now`) refuses the claim at
   `MIMI_TENANT_INFLIGHT` (default 8). The advisory lock needs no tenant row,
   cannot deadlock against the turn lock, and is released with the transaction.
5. One live lease per thread is enforced with a second query, `claim_seq` on
   the thread is bumped, and waiting turns on the thread are merged into this
   turn's batch (`state = 'merged'`, `merged_into`).
6. The claim is a compare-and-set on `turns.version`:
   `state = 'leased'`, `lease_owner`, `lease_expires_ts = now + lease_s`,
   `attempts + 1`, the batch. A lost CAS rolls the whole transaction back.
7. Inbox rows for the batch are released to this turn and one `turn_attempts`
   row records who claimed it, when, and whether it was a takeover.

SQLite has one writer at a time: the same transaction path runs under a
process-wide lock plus SQLite's own write lock. The version compare-and-set is
the same.

Statement for operators: Postgres runs the claim at its default **READ
COMMITTED** isolation. `FOR UPDATE SKIP LOCKED` is what makes two workers
unable to claim one turn: the second transaction either sees the committed
`leased` row and skips it, or locks it first and the other skips. It also
means a slow worker holding a turn or a thread row cannot block a fast worker
from claiming different work. READ COMMITTED is enough because every state
change is a conditional update guarded by `version`, not a read-then-write
based on an earlier snapshot.

## Visibility timeout and takeover

A lease is a visibility timeout: `lease_s` (default 30 s, `MIMI_LEASE_S`, the
same setting `Agent.from_env` passes in). The holding worker heartbeats every
`lease_s / 3`; a heartbeat updates `lease_expires_ts` for its own turn only.

If the worker dies, its lease lapses and the next claim of that turn is a
**takeover**: `attempts` goes up, `turn_attempts.takeover = 1`, and the old
worker's compare-and-set writes (`checkpoint`, `finish_in`) raise `LeaseLost`
and write nothing. The turn resumes from its last checkpoint, so no completed
step is redone twice. `tests/test_worker_kill.py` proves this with a real
SIGKILLed subprocess; `tests/test_queue_reliability.py` proves the queue-level
race and the dead holder's writes.

## Attempts, backoff, poison

Two separate limits:

* `MAX_ATTEMPTS = 3` (`release`): a step raised. The turn goes back to
  `queued` with `lease_expires_ts = now + min(30 s, 1 s * 2^(attempts - 1))`,
  so retries wait 1 s, 2 s, 4 s and never more than 30 s. The third failure is
  `state = 'failed'` with the error words.
* `MAX_CLAIMS = 6` (claim): a worker keeps dying while holding the turn, so
  `release` never runs. The next claim attempt parks it as
  `state = 'failed'`, `status = 'poisoned'`,
  `error = "poison: claimed 6 times without finishing"`, and writes exactly one
  owner note (`kind = 'poison_note'`, idempotency key `poison:<turn_id>`).

A poisoned turn is never a claim candidate again: it cannot spin or fill logs.
The note tells the owner Mimi could not finish and a person should look. Both
kinds of failure are the **dead letter**: `state = 'failed'`.

## Dead letter and one-command replay

Inspect:

```bash
python -m mimi.store queue list                 # parked turns
python -m mimi.store queue list --tenant coral-lane --json
python -m mimi.store queue stats --json         # depth, leases per tenant, dead letter count
```

Rows show `turn_id`, tenant, attempts, status (`poisoned` or `failed`) and the
error words. `--db` overrides `DATABASE_URL`/`MIMI_DB_URL`.

Replay one:

```bash
python -m mimi.store queue replay --id turn_0123456789abcdef
```

It refuses anything that is not parked (`exit 1`, nothing changed), and for a
parked turn it is one transaction: `state = 'queued'`, `attempts = 0`, lease
cleared, `stop = false`, `version + 1`. The turn id and its idempotency keys
stay the same, so the rerun resumes from its checkpoint and completion is the
same one compare-and-set (`completions` still increments once). The poison
note is not duplicated: its idempotency key already exists. Replay is also
available in Python as `TurnQueue.replay(turn_id)`; it raises
`NotDeadLettered` and leaves the row untouched otherwise.

Replay is deliberately manual. A person sees the error, fixes the cause
(the model answer, a tool, data), then replays. Automatic replay of a poison
turn is how a bad job becomes an outage.

## Idempotency at the boundary

Work is keyed before it can be duplicated:

* Inbound messages have primary key `(tenant_id, external_id)`; `Agent.submit`
  inserts with `insert_ignore`, so a redelivered webhook adds no turn.
* `turn_id_for(tenant, external_id)` is a stable sha256, so the same message
  maps to the same turn even if submitted twice.
* Connector writes carry `messages.idempotency_key`, and the book (`SqlGo`)
  stores intents under that key: a replay or takeover submits the same key and
  the second write is observed, not applied again.
* Owner-visible side effects use fixed keys (`poison:<turn_id>`), so a
  takeover or replay cannot send the same note twice.

Replay never changes a turn's key. That is the point: the safe rerun is the
same work, not new work.

## Per-tenant fairness

`MIMI_TENANT_INFLIGHT` (default 8) caps live leases per tenant in the claim
transaction. One distributor with a large backlog can therefore use at most
that many workers; the remaining workers still claim other tenants' turns.
The cap is per worker process? No: on Postgres the advisory lock and the count
cover every process using the same database, so the number is cluster-wide.
On SQLite the lock is per process and the cap is best effort across processes
(SQLite deployments are single-process by design). `TurnQueue(tenant_cap=...)`
overrides the environment for tests and one-off runs.

## Worker process

```bash
python -m mimi.api worker [--db URL] [--idle 0.5] [--health-port 8081]
```

* SIGTERM/SIGINT set a `draining` flag and a `stop` flag: the worker finishes
  the turn in its hands, heartbeats while it does, then stops claiming and
  exits cleanly. `run_worker` never starts a new turn after `stop` is set.
* `--health-port` (or `MIMI_WORKER_HEALTH_PORT`) serves:
  * `GET /livez`: the process is up (200, no database).
  * `GET /healthz`: 200 only when the worker can claim (a database round trip
    and the claim's own bounded read) and is not draining. Otherwise 503 with
    `can_claim: false` or `draining: true`, plus `queue_depth`, `dead_letters`
    and `checked_at`. A process whose database is gone is not healthy.
* `python -m mimi.api serve` keeps the API's own worker threads; scale worker
  processes beside it with `MIMI_WORKERS=0` on the API.

## Counters for observe

`TurnQueue(..., on_event=fn)` calls `fn(name, fields)` for `claim`, `poison`
and `replay`. `stats()` returns depth, per-state counts, oldest queued
timestamp, live leases per tenant and the cap. `mimi/observe` can wrap the
hook and export counters without the queue depending on a metrics library.

## Known limits

* The per-tenant cap counts leases, not CPU: a worker holding several turns
  counts several times. It bounds concurrent turns, which is what matters for
  fairness.
* `claim()` scans up to 500 candidates. A tenant at its cap is skipped in the
  scan, but if every visible candidate belongs to capped tenants the poll
  returns empty and the worker sleeps `idle_s`. A deeper backlog is picked up
  on the next poll, not starved permanently.
* The poison note is written inside the claim transaction; if the database
  refuses it, the turn stays leased until the next claim retries the poison.
* Replay clears `attempts`. A turn that poisons repeatedly can be replayed
  repeatedly by a person; that is an operator decision, not a loop.
* Rollback: the queue is an in-place hardening of the existing `turns` table,
  with no schema change. Reverting the code restores the previous claim path;
  rows are compatible in both directions.

## Tests

* `tests/test_queue_reliability.py`: killed holder taken over, poison parks
  once, replay runs once, two workers race one job, 12-turn burst across four
  workers claimed exactly once each, tenant cap, probe/stats.
* `tests/test_worker_health.py`: health reflects claimability and draining,
  HTTP endpoints, SIGTERM finishes the turn in hand and stops claiming.
* `tests/test_worker_kill.py` (existing): real subprocess SIGKILLed mid-turn,
  two workers, every turn `done` with `completions == 1`.
* `tests/test_loop_safety.py` (existing): a job that keeps killing its worker
  is poisoned once.
