# Data: budgets, indexes, pooling, migration safety and retention

Pilot calibration: 10,000 shops, tens of distributors. Written 11 Nov 2026.

## Query budgets

A budget is a number a reviewer can check, not an aspiration. Every hot path has
a target, every statement is measured, and a path that exceeds its p95 is
flagged rather than discovered by a customer.

- `mimi/store/budgets.py` holds the table: path -> (`max_statement_ms`,
  `p95_path_ms`, what the query is for).
- Production logs a WARNING for any statement over `MIMI_SLOW_QUERY_MS`
  (default 200 ms).
- `scripts/plan_audit.py` runs the hot queries and marks any path whose p95
  exceeds its budget.

Representative paths, with the reason the budget is what it is:

| Path | Statement | Path p95 | Why |
|---|---|---|---|
| `invoices.open` | 30 ms | 30 ms | one shop's open invoices, locked in due order |
| `book.bank_feed_match` | 30 ms | 30 ms | match one credit by amount during a shop's message |
| `queue.claim` | 30 ms | 30 ms | the claim must be cheap: it runs per worker per tick |
| `context.history` | 30 ms | 30 ms | one thread's history in time order, per turn |
| `console.threads.list` | 60 ms | 60 ms | a page of threads with counts |
| `console.shop.open` | 60 ms | 60 ms | one shop with deliveries, invoices and promises |
| `credit.receivables` | 60 ms | 60 ms | the whole open book, oldest first |

Two known offenders are recorded in `budgets.py` with the fix rather than hidden:
a `console.tasks.list` that degenerates with a large page, and a returns read
that returns every shop unpaginated. Both are marked over target and tracked.

## Indexes (`0019`)

Every index names the query it serves. Three are **replacements**: the new index
begins with the old one's columns, so keeping both would only add write cost.

| Index | Table | Columns | Serves |
|---|---|---|---|
| `ix_threads_tenant_last` | threads | tenant, last_ts, thread_id | thread list, newest first |
| `ix_turns_tenant_created` | turns | tenant, created_ts | turns by tenant and day |
| `ix_messages_tenant_idem` | messages | tenant, idempotency_key | duplicate suppression |
| `ix_approvals_tenant_thread_status` | approvals | tenant, thread, status | a thread's open cards |
| `ix_thread_events_tenant_turn` | thread_events | tenant, turn | one turn's steps |
| `ix_inbound_messages_tenant_turn` | inbound_messages | tenant, turn | message -> turn |
| `ix_inbound_messages_tenant_thread` | inbound_messages | tenant, thread | a thread's messages |
| `ix_receivables_tenant_open` | receivables | tenant, open, due | the open book |
| `ix_payments_tenant_day` | payments | tenant, day | payments on a Lagos day |
| `ix_bank_feed_tenant_amount` | bank_feed | tenant, amount_kobo | match a credit by amount |
| `ix_disputes_tenant_shop_status` | disputes | tenant, shop, status | a shop's open disputes |
| `ix_promises_tenant_shop` | promises | tenant, shop | broken promises |
| `ix_proof_events_tenant_record` | proof_events | tenant, record, at | a proof's history |
| `ix_history_tenant_thread_ts` | history | tenant, thread, ts | context rebuild |
| `ix_compactions_due` | compactions | due_ts (partial: version = 0) | due timers only |

On Postgres every index builds `CONCURRENTLY` in an autocommit block, so a deploy
does not hold a write lock on a growing table. SQLite builds inline, which the
offline tests accept.

## Connection pooling

`mimi/store/db.py`: bounded queue pool, `MIMI_DB_POOL_SIZE` (5) plus
`MIMI_DB_MAX_OVERFLOW` (5), waiting at most `MIMI_DB_POOL_TIMEOUT_S` (5 s), recycling
connections after `MIMI_DB_POOL_RECYCLE_S` (1800 s), `pool_pre_ping` on.

The arithmetic that matters: **per process** the ceiling is 10 connections. With
N workers plus the API process, the total must stay under the database's
connection limit with headroom for migrations and an operator session. On Neon,
use the pooled (PgBouncer) host for the app and the direct host for migrations;
`MIMI_DB_POOL_SIZE=0` or a negative overflow is a typo and is refused rather than
treated as a policy.

## Migration safety (`mimi/store/migration_safety.py`)

Static: it reads the revision files with `ast` and never imports or runs one. CI
runs it; a violation fails the build with the rule named.

1. **One head, one parent.** Duplicate ids, branches and merge revisions fail.
2. **Reversible, or a stated reason.** A no-op downgrade needs
   `IRREVERSIBLE = "reason"`. A downgrade that raises is never allowed, because CI
   runs `alembic downgrade base`.
3. **Expand and contract never share a deploy.** A destructive operation needs
   `EXPAND_CONTRACT = "<earlier revision>"`, naming the deploy that stopped
   reading or writing the thing, and that revision must be an ancestor.
4. **No long lock on a growing table.** Plain `CREATE INDEX`, a `NOT NULL` column
   without a default, or an `alter_column` on a table not listed in
   `SMALL_TABLES` fails. Unknown tables are treated as growing.
5. **A migration does not delete data.** A raw `DELETE`/`UPDATE` needs
   `DATA_MIGRATION = "reason"`; retention belongs in `retention.py`.

Run it by hand with `python -m mimi.store.migration_safety`.

## Retention

Retention is a job (`python -m mimi.store.retention [--apply]`), never a migration.
Every period has a product reason; the longest is dispute evidence.

| Table | Column | Days | Reason |
|---|---|---|---|
| turn_attempts | claimed_at | 180 | operational noise, not evidence |
| inbound_messages | received_at | 730 | two years of what a shop actually said |
| turns | created_ts | 730 | same window as the messages they answer |
| thread_events | ts | 730 | step history for the same window |
| history | ts | 730 | context rebuild history |
| digest_queue | created_at | 400 | a digest is useful for a season |
| speak_counts | created_at | 400 | per-shop speaking caps |
| routine_runs | started_ts | 400 | routine history |
| messages | ts | 2190 | six years: a dispute or an audit can reach back this far |
| proof_events | at | 2190 | six years: proof of what was confirmed and when |

`DISPUTE_EVIDENCE_DAYS` is six years and `check_policies()` refuses a shorter
period for anything that carries evidence, so a future change that shortens
evidence retention fails rather than silently deleting it.
