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
| `inbound.py`, `frontdoor.py` | The front-door envelope, the directory, dedupe, threads, lanes, batching, run rows (RAI-280) |
| `router.py`, `lexicon.py`, `fastclass.py`, `skills.py` | The router: rules, multilingual phrases, the distilled fast classifier, Muse Spark, guards, clarify templates; the skill catalogue (RAI-280) |
| `context.py` | The context builder: pinned prefix, history window, book slices, budgets, manifest, artifacts, image guard (RAI-281) |
| `wiring.py` | A routed turn to a harness run with its built context |
| `router_eval.py`, `context_eval.py` | The router and context evals with their pass marks |

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

## Router (RAI-280): the front door and the pick

Built 27 Oct 2026. `mimi/harness/inbound.py`, `frontdoor.py`, `router.py`,
`lexicon.py`, `fastclass.py`, `skills.py`, `wiring.py`.

```
Inbound {source, org_id, thread_id?, external_id, received_at, payload}
   |  FrontDoor.submit: drop a repeated (org_id, external_id); pick the thread
   |  (shop, owner or job thread; unclear -> owner thread with a note); queue it
   |  fast lane: owner 1:1 messages start at once, steer a running owner turn,
   |             "stop" interrupts every running turn of that distributor
   |             (owner = no number (console) or a number on owner_phones; an
   |             owner-channel message from any other number is a stranger:
   |             no stop, no steering, its own new:<pseudonym> thread, untrusted
   |             in the prompt). Steering the turn never read starts the next
   |             turn; a stop cancels steering sent before it.
   |  unknown numbers: thread id new:<HMAC-SHA256 of the digits, 12 hex>, keyed
   |             by MIMI_PHONE_PSEUDONYM_KEY (required when MIMI_ENV=production)
   |  batch lane: 1.5 s quiet window (6 s at most); a burst is one turn
   |  one turn per thread at a time; threads run in parallel; owner first
   v
Router.route_batch(events, directory, thread_state) -> RouteDecision
   1 rules (plain code)   unknown number -> onboarding; routine / check-in /
                          webhook / helper report / approval -> its helper;
                          owner "stop"; owner @Helper; answer to a waiting
                          question (0-2 | 3-5 | 6+, YES, a number); quick-reply
                          button; shop STOP; screenshot -> Proof
   2 phrases              multilingual phrase rules (en, pcm, yo, ha, ig) +
                          SMS spellings; trusted when one skill clearly leads
   3 fast classifier      naive Bayes on char n-grams, distilled from 2,708
                          Muse Spark-written messages; trusted only where safe
   4 Muse Spark           JSON over the sender's skill catalogue, the message
                          in untrusted tags (reasoning_effort=minimal)
   guards: injected sentences and shop @mentions removed before routing;
           with an injection the model cannot lift the route above what the
           shop's own words support; per-helper thresholds (money higher);
           below -> one question with two buttons, thread parked; a second
           unclear answer goes to the owner, never a second question
   v
{helper, skill, confidence, clarify_question?, options, layer, rule, language,
 secondary, lane, source, external_id, latency, model spend, events}
   events: "Handed to Collector" (handed_to), asked_to_clarify, stopped,
           security - the console shows them centred and grey
```

Helpers and skills (`skills.py`, 20 skills): Order (`order-intake`,
`drop-confirmation`, `delivery-status`), Collector (`collections`,
`payment-check`, `promise-to-pay`, `chase`), Proof (`fake-alert-check`,
`bank-credit`), Credit (`credit-request`, `credit-review`), Stock
(`stock-count`, `low-stock-report`; PR #7's one-tap answers route here by
rule), Brand (`promo-info`, `brand-email`), Mimi (`shop-onboarding`,
`owner-brief`, `chat`, `escalate-owner`, `stop`). Money skills (balance,
payment, promise, chase, Proof, Credit) are where a misroute counts as a
money misroute. Helper risk for the injection guard: Mimi 0, Brand and Stock
1, Order 2, Proof and Collector 3, Credit 4.

Thresholds (`THRESHOLDS["v1"]`): Mimi 0.5, Brand and Stock 0.6, Order 0.65,
Proof and Collector 0.7, Credit 0.75. A new set ships behind
`MIMI_ROUTER_THRESHOLDS`; the fast classifier is on unless the router is
built without it.

The fast classifier is trusted when it agrees with phrase evidence (prob
>= 0.9), or, with no phrase evidence, when it is >= 0.99 sure of a non-money
skill on a message of three or more words in English or Pidgin (its Yoruba,
Hausa and Igbo lines are too thin to trust alone). A one- or two-word message
nothing matched is "too short to tell": one question, with buttons from what
the message is made of ("50k": a payment you made or when you will pay;
"Friday": when you will pay or a late delivery; "Indomie", "2 carton": an
order or what is left on the shelf), else the classifier's two best guesses.

`wiring.TurnRunner` turns a routed turn into a harness run: system events to
the feed, the clarifying question when there is one, the context built for
the routed skill with the wake source and every external id on the run, the
loop, the turn's messages into history, and the thread left waiting when the
helper asked a question. Helpers not hired yet get the handoff event and the
turn reports `no_helper_yet:<helper>`.

### Router eval (`python -m mimi.harness.router_eval`)

300 hand-written synthetic messages (`eval/router/messages.jsonl`): 77
structural (routines, webhooks, approvals, helper reports, unknown numbers,
@mentions, stop, waiting answers, buttons, screenshots), 139 shop texts
across five languages with typos, 32 owner texts, 8 rep texts, 16 voice
transcripts, 6 camera photos, 12 prompt injections, 10 genuinely ambiguous
(where one question is the right answer: a direct route scores as wrong, and
as a money misroute when it lands on a money skill, unless the case lists it
in `safe_direct`). Dev/test split by id hash (92/208);
thresholds and the fast-classifier rules were set on dev. Muse Spark 1.3
answers recorded once (27 Oct 2026) and replayed in CI.

| | Accuracy | Clarify rate | Money misroutes | Model share | p95 latency |
|---|---|---|---|---|---|
| Pass mark | >= 95% | <= 8% | 0 | | < 800 ms |
| **Cascade (ships)** | **98.7%** (dev 92/92, test 204/208 = 98.1%) | **4.3%** | **0** | 5.0% | **4 ms** |
| Model only (rules + Muse Spark for every text) | 97.0% | 6.0% | 0 | 73% | 1,737 ms |
| Rules + phrases, no model | 90.7% | 12.7% | 0 | 0 | 0.4 ms |
| Rules + phrases + fast classifier, no model | 95.3% | 8.0% | 0 | 0 | 0.8 ms |

Also gated: rules cases never call the model (0 of 77 did); unknown numbers
always onboarding (10 of 10); no injection raised the route's risk (0 of 12).
Model-path latencies are the recorded live ones (median about 1.0 s, so the
p95 holds only because 95% of decisions never wait on the model; the margin is
thin: 15 of 300 decisions called the model). The four misses: a Yoruba
"not complete, two cartons remain" the model read as a stock count, a
two-word Igbo stock message that asked, and two injection cases the guard
turned into a question (safe, but counted as misses).

## Context builder (RAI-281): the minimum context per run

`mimi/harness/context.py`. `ContextBuilder.build(event, helper, skill)` ->
`Context {messages, tools, manifest, prefix}`, in this order:

1. system prompt: core rules + the helper's one skill
2. tool definitions (the API's `tools`)
3. memory snapshot (owner style, the shop's habits; `MemoryAPI`, RAI-236)
4. routines list
5. environment: distributor, helper, thread, shop, the day the thread opened, tool names
6. history, append only from a window start that jumps forward in one big
   step when the budget is reached (never one turn at a time), plus the
   thread summary from compaction (RAI-285) when there is one
7. newest message: the time (WAT), the routed skill's one-line focus, notes
   on what changed since the last turn, the consent-checked book slice read
   now through Go, other helpers' verdicts (text only), then the input

Sections 1 to 5 are pinned per (tenant, thread, helper) in `PrefixStore` and
stay byte-identical until compaction; a deploy that changes a tool
description leaves a running thread on its old prefix with a note on the
newest message ("tools updated since this thread began: ..."), and new
threads get the new prefix. Each section is hashed; the hashes ride on
`run_started` and every `model_called` event and on the trace.

Book slices per skill (`SLICES`): collections reads balance, open invoices,
recent payments and promises; payment-check balance, invoices, payments;
promise-to-pay balance, invoices, promises; fake-alert-check invoices and
payments; credit-request balance, payments, promises; order-intake balance;
drop-confirmation open invoices. Everything else reads through its tools.
Each read is checked against the shop's grant; a missing grant is listed in
the manifest as omitted. Budget: 8,000 input tokens per helper by default;
history is trimmed first, then slice rows, then the slice; a pinned prefix
that does not fit fails safe (`ContextError`).

Pointers, not payloads: in the loop (`Harness(artifacts=ArtifactStore())`) a
read result over 2,000 tokens is stored and only its pointer, a three-line
summary and its headline scalar fields (totals, counts, `*_kobo` with
`*_naira`) enter the transcript (`artifact_stored` event).
`assert_no_image_bytes` refuses data URLs, base64 blobs and image content
parts in the main context; screenshots and shelf photos go to Proof or
Stock and only their verdict enters.

### Context eval (`python -m mimi.harness.context_eval`)

The 50 Collector scenarios, routed first, on live Muse Spark 1.3 (recorded
once, replayed in CI), against a full-history baseline: the core rules and
skill, the tools, then the whole eight-week thread history (deliveries,
payments, promises, the Collector's ladder reminders, weekly stock counts;
on the owner's thread a daily brief and the whole distributor's book), the
shop's whole book, every memory note and routine. The builder arm is a real
multi-turn replay: the owner's asks share four owner threads of six turns,
shops with several scenarios share their thread, routines run on their job
thread.

| | Pass mark | Builder | Full-history baseline |
|---|---|---|---|
| Task success | builder >= baseline | **50/50** | 46/50 (needed an N300 run budget; 50/50 runs cost over N50) |
| Median input tokens per run | down >= 50% | **10,179** | 54,432 (**-81%**) |
| Median first-call tokens | | 4,808 | 21,252 |
| Model spend, all 50 | | N1,177 | N5,332 |
| Provider-reported cached input tokens | >= 80% | **81.4%** | 34.4% |
| Prefix-cache estimate, 50 scenarios as 22 multi-turn threads | >= 80% | **88.7%** (72.6% same-thread only) | |
| Context over budget | 0 | **0** (max 7,942 of 8,000) | |
| Manifest on runs | 100% | **50/50** | |

Offline marks, all passing: cross-distributor leakage with two distributors
sharing one shop (26 cases, 0 leaks; a negative-control test proves the
check catches a leak); sections 1 to 5 identical across 20 turns; a deploy
keeps the running thread's prefix, notes it, and new threads get the new
one; a 400-row tool output enters as a 95-token pointer; image bytes and
image parts refused; a shop with nothing open, a shop that shares nothing,
an unknown shop and a run with no shop each get a valid context.

A second, independent live recording of the builder arm (not committed) also
scored 50/50 with 85.5% provider cache hits, so the result is not one lucky
sample. The two Collector-routed-elsewhere cases are delivery disputes
("the last delivery was short", "the cartons were damaged") that the router
sends to Order, which is arguably right; the scenarios still run Collector.

## Not yet

* Go itself: `FakeGo` stands in for the consent-checked book, the policy
  endpoint, the event log, the intent API and the dedupe index. The ports
  are the contract; the front door's queue, thread lock and dedupe are
  in-process stand-ins for Postgres (advisory lock or a River unique job per
  thread) and Go's unique index.
* Compaction (RAI-285): the builder has the hook (`PrefixStore.compact`,
  `HistoryStore.set_summary`) and the eval seeds a code-written summary; no
  model writes summaries yet.
* Memory (RAI-236): `MemoryAPI` is a port; the eval uses notes derived from
  the synthetic world.
* Only the Collector is a full hire; Proof is a minimal config. The other
  helpers are routed to and get the handoff event, then report
  `no_helper_yet` until RAI-272 moves the Day 2 to 6 handlers behind the
  router as skills.
* The kill switch and daily cap live in the control-plane port; there is no
  console toggle yet.
