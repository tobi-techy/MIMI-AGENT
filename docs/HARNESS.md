# Agent harness (`mimi/harness/`)

Built during the hackathon, 27 Oct 2026, for RAI-278 (agent loop with step,
time and cost budgets) and RAI-279 (typed tool registry with scope, risk tier
and idempotency). In-house Python, no agent framework, model-agnostic through
OpenAI-style tool calling.

The one rule: **Mimi proposes, code checks, the owner approves.**

## Shape

```
RunEvent (owner ask, shop message, routine, webhook, ...)
   |
Harness.run(event, helper)  =  Harness.step(state) until the turn ends
   |  gates before every step: helper flag, kill switch (helper, distributor),
   |  daily naira cap per distributor (owner asked once a day), run budgets
   |  (steps 12, seconds 60, naira N50, with the next call's worst case reserved)
   |
   |  Gateway: primary model, fallback chain on a provider error, cost in kobo
   |  dispatch each tool call:
   |    allow list -> parse -> drop fields the model may not set (tier, approved ...)
   |    -> Pydantic validation (one repair, then fail safe)
   |    -> connector-down rule (no web or computer fallback)
   |    -> consent grant for the scope (typed Denial, never data)
   |    -> read: Go's consent-checked book API, naira text added by code
   |    -> write: an IntentRecord (tier from Go policy, business and attempt
   |       keys, dry-run preview); approve tier ends the turn
   |  speaking is a tool (send_to_user / ask_question); figures in a message
   |  must appear in a tool result or the owner's words
   v
RunResult(status, intents[], outbound_drafts[], events[], owner_question, spend, ...)
```

Statuses: `done`, `done_quiet`, `parked_for_approval`, `parked_for_timer`,
`asked_question`, `stopped_budget`, `stopped_killed` (added: kill switch or
helper flag), `failed`.

## Modules

| Module | What it holds |
|---|---|
| `types.py` | Pydantic records that cross to Go: `Event`, `IntentRecord`, `OutboundDraft`, `RunEvent`, `RunResult`, `StepResult`, `Denial` |
| `loop.py` | `Harness` (`start`, `step`, `run`, `resume`), `Helper`, `RunBudget`, `LoopState`, core rules, the figure guard, the naira formatter |
| `registry.py` | `@registry.tool(...)`, `ToolSpec`, `Toolset`, `build_toolset` (the startup check), schema export, the toolset hash |
| `policy.py` | `GoPolicy`: tiers per tool, the owner's N50,000 auto limit, `never` for money and bank details, fail closed for unknown tools |
| `go.py` | Ports to the system of record (`BookAPI`, `ControlPlane`, `EventLog`, `IntentGate`), `ConsentGrants`, and `FakeGo` over the synthetic world |
| `gateway.py` | `OpenAIToolModel` (httpx, Muse Spark tool calling), `Gateway` (fallback chain), `kobo_cost`, `ScriptedModel`, `RecordedToolModel` |
| `builtins.py` | `send_to_user`, `ask_question`, `update_plan` |
| `collector.py` | The 13 Collector tools, the Collector skill, a minimal Proof helper |
| `durable.py` | `harness_steps(...)`: the loop as a step of the always-on `JobRunner` |
| `tracing.py` | One Langfuse trace per run, a span per step |
| `scenarios.py`, `eval.py` | The 50 Collector scenarios and every suite with its pass mark |

## Tools

Every tool declares: name, description, input model, output model, kind
(`read` or `write_intent`), scope (`shop:balance`, `org:receivables` ...),
a declared tier, idempotency fields, a dry run (required for write tools),
a cost tier (`knowledge`, `go_api`, `web`, `computer`, `ask_owner`) and the
connector it needs, if any.

```python
registry.tool(kind="write_intent", scope="shop:messages", tier="notify",
              idem=("shop_id", "invoice_id", "date"), dry_run=_reminder_dry)(send_reminder)
```

Collector's tools: `get_balance`, `get_invoices`, `get_payment_history`,
`get_account_number`, `check_payment` (reads), and `create_exact_amount_account`,
`send_receipt`, `propose_reminder_plan`, `send_reminder`, `set_followup`,
`ask_helper`, `notify_rep`, `notify_owner` (write intents), plus the three
built-ins. Reads call Go's book with the run's tenant only; every amount in an
output is read from the book or summed in integer kobo, and each `*_kobo`
value gets a `*_naira` text beside it written by code, so the model copies
figures and never converts them.

Rules the registry and loop enforce:

* A tool outside the helper's allow list is refused and logged (`tool_refused`).
* A toolset that lists a `never` tool (or a name with a money verb) fails at startup.
* The tier is read from Go policy for every call; `tier`, `approved`,
  `skip_approval` and similar fields the model sends are dropped and logged.
* Each tool's description plus schema stays under 4,000 characters (test).
* Every error says what to do next (`... Next step: ...`); a test lints them.
* The toolset is fixed per helper and its hash only changes with a deploy.
* With a connector down, web and computer tools are switched off for the
  turn and the helper asks the owner.
* The summary-only schema mode (`build_toolset(..., full_schemas=False)`) is
  behind a flag; the full schema loads on the tool's first use.

## Idempotency

`IntentRecord.idempotency_key` is the business key: tenant, intent kind and
the tool's declared fields (the same reminder for the same shop, invoice and
Lagos day has one key, whichever job proposes it). `attempt_key` adds the job
and the step, and the runtime outbox adds its own key per step and position.
Go (here `FakeGo`) dedupes on both, so a transport resend, a crash after Go
accepted, a crash mid-step or a second job re-proposing the same thing
applies once. Inside one turn the same intent is proposed once.

## Durable steps

`harness_steps(harness, {"collector": helper})` gives the `JobRunner` a step
function. The loop state (transcript, step log, spend, proposals) is saved in
`Job.scratch` in the same compare-and-set as the step, the plan, to-do and
working note in the job's own fields. A new worker resumes from the store; a
parked run (question, approval, budget, timer) reopens when the job wakes. In
step mode the runtime enforces the job's step, time and naira budgets (with
the owner's "yes" grant) and the harness enforces the daily cap and kill
switches.

## Evals (`python -m mimi.harness.eval`)

Recorded on 27 Oct 2026; report in `eval/harness/harness.report.json`.

| Suite | Pass mark | Result |
|---|---|---|
| 50 Collector scenarios, scripted policy (harness mechanics, with provider 5xx, bad-argument and tier-spoof faults) | completion >= 95% | **50/50** |
| same, live Muse Spark 1.3 (recorded, replayed in CI) | completion >= 95% | **50/50** |
| Side effects from inside the loop (both) | 0 | **0** |
| Runs over budget without `stopped_budget` (both, plus 6 runaway models) | 0 | **0** (6/6 runaways stopped within limits) |
| Median model calls per turn (live) | <= 2 | **2** (36 runs at 2, 12 at 3) |
| p95 loop overhead excluding model time | < 150 ms | about 13 ms scripted, 19 ms live replay |
| Security: cross-distributor reads, missing consent, injected tool names, tier spoofing, never-tier registration | 100% refused | **41/41** |
| Replayed retries (resend, crash after accept, crash mid-step, second job) | 0 duplicates | **200 retries, 0 duplicates, 0 lost** |
| Idle distributor for 24 h with one routine | 0 calls outside routines | **0** (1 call, the routine) |

Live cost on Muse Spark 1.3 (N1,600 per USD, a planning rate): median N18.86
per run, highest N37.65, about N1,029 for all 50 (about USD 0.64). Every
scenario's expected end state is computed from the world in code. One
expectation is wider than the rest: "I go pay on next week Tuesday" accepts
one clarifying question as well as a follow-up timer, because the phrase is
ambiguous and asking once is the rule (RAI-232); the model asked.

What the scripted numbers measure is the harness; what the live numbers
measure is the agent on these 50 synthetic cases. Neither is a pilot result.

## Tracing

`trace_run(result, exporter)` exports one trace per run (`run-collector`),
session = thread, a `chain` span per step with the model generation and each
tool call (tier, scope, idempotency key), and scores `run_status`,
`stopped_budget`, `side_effect_in_loop`, `model_calls`, `intents`. Metadata
carries the wake source, toolset hash, registry and skill versions.
`make trace-harness` sends four replayed runs (no model cost). In durable
mode the runtime's step spans carry `tool`, `short`, `duration_ms`,
`cost_kobo` and `wake_source`.

## Not yet

* Go itself: `FakeGo` stands in for the consent-checked book, the policy
  endpoint, the event log and the intent API. The ports are the contract.
* Router (RAI-280) and context builder (RAI-281): the event's text goes in
  as is; the owner names the shop id.
* The kill switch and daily cap live in the control-plane port; there is no
  console toggle yet.
* Only the Collector is a full hire; Proof is a minimal config.
