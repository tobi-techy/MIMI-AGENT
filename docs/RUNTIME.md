# Always-on runtime (`mimi/runtime/`)

Built during the hackathon, started 26 Oct 2026.

Always on does not mean a process running all day. A helper job wakes, runs
one step at a time, saves after every step, and parks or sleeps. Anything can
wake it: a shop message, the owner, a routine timer, an event from the system of record. An idle
distributor costs close to nothing; a job can still run for hours across many
wake-ups and survive any number of worker restarts.

## States

```
queued -> working -> done
             |-> waiting_on_owner  (asked a question, budget hit, owner said stop)
             |-> sleeping_until    (timer; an owner message wakes it early)
             |-> blocked           (forbidden intent, 3 failed steps, no helper)
working -> working                 (lease takeover after a crash)
waiting_on_owner / sleeping_until -> queued   (owner message arrives)
```

Anything else raises `IllegalTransition`.

## One step

1. Flush the outbox: intents saved by the previous step (or a crashed worker)
   go to the system of record with their idempotency key. It dedupes, so a
   resend is a no-op (`RecordingIntentSink` and the sim's `SimGo` do the same).
2. Apply the previous step's park/finish decision, if any (only after the
   flush, so a job never parks with unsent intents).
3. Heartbeat the lease. Lost it? Stop without writing.
4. Take owner messages. "stop" parks the job now; anything else is handed to
   this step as `new_owner_messages`. Messages are acknowledged in the same
   save that records them, so a crash never loses or doubles one.
5. Check budgets (steps, working seconds, naira of model spend). A hit parks
   the job as `waiting_on_owner` with a question whose figures come from code.
   An owner "yes" adds `budget_grant` and the job carries on.
6. Run the step inside a span (OpenTelemetry with Langfuse attribute names).
7. Turn its intents into outbox rows with keys
   `sha256(job, step, position, kind, payload)`.
8. Save (compare-and-set on version and lease owner).

## Guarantees and how the tests prove them

| Guarantee | Test |
|---|---|
| A saved step is never run again after a crash | `test_crash_resume_never_redoes_a_saved_step_or_double_sends` |
| An intent is accepted exactly once across crashes | same, plus `test_crash_before_go_accepts_still_delivers_once` |
| A crash mid-step reruns only that step | `test_crash_inside_a_step_reruns_only_that_step` |
| A stale worker can never overwrite a newer one | `test_stale_worker_cannot_write_after_takeover`, `test_sql_claim_race_has_one_winner` |
| Long jobs keep their lease | `test_long_job_heartbeats_between_steps` |
| Budgets park and ask; yes resumes | `test_step_budget_parks_and_asks_then_owner_yes_resumes`, naira and time tests |
| Owner preempts at the next boundary, owner jobs claimed first | owner preemption tests |
| Steps can only emit typed intents for their own tenant | `test_step_returning_a_non_intent_blocks_and_sends_nothing`, cross-tenant test |

All run against the in-memory store and the SQL store (SQLite in CI;
Postgres in production through alembic revision 0001).

## Guardrails

* Mimi never moves money. Steps return typed intents only, and only the types
  their helper registered with `outbox.intent_types`, which refuses any kind
  named for a money verb (transfer, payout, refund and so on). The runtime has
  no intent types of its own; the receipt flow registers three
  (`send_shop_message`, `record_delivery_consent`, `flag_to_owner`).
* Money and stock figures in intents and in the budget question are computed
  in code. Trace attributes are for debugging, never for figures shown to the
  owner.
* The runtime imports only `mimi.types` and nothing above it (enforced in
  `tests/test_architecture.py`).

## Not yet

* Workers next to the system of record: today the worker is Python. The job
  table can move behind another service's API without changing the runner.
* Daily naira cap per distributor and kill switch per helper.
* A soak test of 40 simulated shops for 6+ hours.
* The claim query filters in Python; at pilot scale that is fine, beyond it
  move to `FOR UPDATE SKIP LOCKED`.

## Tracing to Langfuse

Set `OTEL_ENDPOINT` to an OTLP collector that forwards to Langfuse's OTLP
endpoint and use `OtelStepTracer`. Each job is a Langfuse trace (session = the
thread), each step a span with `mimi.outcome`, `mimi.intents`,
`mimi.cost_kobo`, `mimi.wake_source` and the step's own attributes.
