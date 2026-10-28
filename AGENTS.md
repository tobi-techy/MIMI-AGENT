# AGENTS.md: working in MIMI-AGENT

For people and coding agents changing this repo. The product and the rules
behind it are in `README.md` and `docs/`; this file is how to work here.

## The one rule

Mimi proposes, code checks, the owner approves. The model reads and decides
what to do; code does every sum and every check; nothing moves money.

## Commands

```bash
pip install -e '.[dev,otel,langfuse]'
make lint typecheck test      # ruff, mypy, pytest (offline)
make check                    # everything CI runs, offline
make harness-eval             # the harness suites and pass marks
make router-eval              # router: 300 labelled messages, pass marks
make context-eval             # context builder: 50 scenarios vs full history, pass marks
```

CI is offline: no key means FakeModel or recorded answers. Recorded Muse
Spark answers live next to each eval (`eval/*/...cache.json`). A change to a
prompt, a tool description or a tool schema changes the request, so the
replayed eval misses; re-record with the eval's `--live` flag and commit the
cache with the change. Recorded loop answers are also keyed by the Go policy
version (`policy.POLICY_VERSION`) and the helper's `skill_version`, and router
answers by `ROUTER_PROMPT_VERSION` and `CATALOGUE_VERSION`
(`mimi.harness.versions`): bump one and its replays miss as `stale` until
re-recorded. A new helper factory calls `versions.register(helper)`.

## Adding a tool (`mimi/harness/`)

1. Write `fn(ctx, args: InModel) -> OutModel` with Pydantic models
   (`extra="forbid"`), a one-paragraph docstring (it is the description) and
   money as `*_kobo` integers read from the book or summed in code.
2. Register it with `registry.tool(kind=..., scope=..., tier=..., idem=(...),
   dry_run=..., cost_tier=..., connector=...)`. Write tools need `idem` and a
   dry run, or registration fails.
3. Add its tier to `mimi/harness/policy.py` (`BASE_TIERS`); a tool with no
   policy row is `never`. Moving money and changing bank details are
   `never` for every helper and cannot be put in a toolset.
4. Add it to the helper's allow list. Never add or remove a tool for one turn.
5. Errors are `ToolError(message, next_step)`: always say what to do next.
6. Keep description plus schema under 4,000 characters (a test checks).
7. Add a test, and a scenario to `mimi/harness/scenarios.py` when it changes
   what a helper does; rerun `make harness-eval`.

## Routing and context (`mimi/harness/router.py`, `context.py`)

* Every wake-up enters as an `Inbound` through the `FrontDoor`; never call a
  helper around it. Rules come before any model call; a rule case that calls
  the model fails the router eval.
* A new skill goes in `skills.py` (helper, name, a routing description,
  senders, `money`), with phrase rules in `lexicon.py` if shops say it in
  fixed ways, labelled cases in `eval/router/messages.jsonl`, and a slice in
  `context.SLICES` if it needs the book. Re-record with
  `python -m mimi.harness.router_eval --live` (a few naira) and commit the
  cache.
* Skills (RAI-314, `MIMI_SKILLS`, default off): each skill is a folder in `skills/`
  (SKILL.md, scripts/, references/, assets/, tests/), locked by hash in
  `skills/skills.lock.json`. Its description must match its `skills.py` row word for
  word. Scripts are pure, typed, audited read tools; the model never writes or runs
  code. Change a skill, then `python -m mimi.skills refs && python -m mimi.skills lock`.
  See `docs/SKILLS.md` and `docs/SKILL-CREATOR.md`.
* Tune thresholds on the `dev` split only; ship a new set behind
  `MIMI_ROUTER_THRESHOLDS`.
* Never put anything that changes per turn in sections 1 to 5 of the
  context; it goes in the notes on the newest message. A change to the core
  rules, a skill or a tool description changes every recorded request:
  re-record the harness and context evals.
* Shop text never reaches a model unmarked: it is wrapped in `untrusted`
  tags, with injected sentences and shop @mentions removed before routing.
  Every wrapper defangs its text with `mimi.harness.untrusted.defang` (any
  tag shape, so a new wrapper is covered without a list).
* A burst that mixes the owner (or Mimi) with outside text is stripped and
  tagged per event and runs untrusted (`mimi.harness.batch`); a rep number
  Go does not know never lands on the owner thread.
* The pinned prefix (sections 1 to 5) is keyed by the run's shop: a thread
  shared by shops re-pins when the shop changes.

## Hard rules

* Write tools return intents only; the loop never writes to the book.
* Shadow mode records intents and applies none: Go (fake and SQL) marks
  them `shadowed`, and the API writes no message, approval or credit row.
* Every tool input model sets `extra="forbid"` (the registry refuses one
  that does not); a bug in a tool, its scope or its dry run fails safe.
* The figure check covers every tool whose text reaches a person or helper
  (`loop.FIGURE_CHECKED`), not only `send_to_user`. It is part of the
  verification gate (`mimi.harness.claims`): figures, dates and shop or
  product names must come from this run's records or the owner's own words
  (never shop text, RAI-313); "done", "sent", "recorded", "approved" need a
  confirmed tool outcome in the run; a promise needs a follow-up set in the
  run; a claim that money moved is always held. Run `make claims-eval`.
* Traces carry no private model text, no hardcoded tenant tag, and no long
  digit run, as text or as a number.
* The loop cannot run away (`docs/LOOP-SAFETY.md`): repeated or failing calls,
  steps with no progress and flapping tools end the run `stopped_runaway` with
  a code-written owner note; `ask_helper` never hands work back up its chain or
  past depth 2, and a child job runs inside its root's budget; follow-ups and
  messages to one shop are capped; failed steps back off and poison jobs park;
  a resumed run keeps its budget unless the owner granted more. Run
  `make loop-safety-eval` after touching the loop, the runner or the queue.
* Risk tiers come from policy, never from the model.
* Go (fake and SQL) refuses any tool with no row on the policy allowlist
  (`mimi.harness.policy.go_allows`), whatever tier the record carries, and
  any tool or kind with a money word (`MONEY_VERBS`: transfer, payout, refund,
  wire, settle, remit, debit and the rest).
* An injected sentence never moves a route to a money skill the shop's own
  words do not name, inside the same helper too (`injection_cap`).
* Production refuses SQLite and the Makefile's demo Bearer keys
  (`Settings.check`); `make api` under `MIMI_ENV=production` fills in no
  defaults.
* Speaking is `send_to_user`; plain model text is private.
* Every outbound message passes the style guard (`mimi.harness.style.clean_reply`:
  no em or en dashes, no AI filler, figures never changed). How Mimi writes is in
  `docs/MIMI-VOICE.md`; code templates follow it.
* Shop, rep and screenshot text is data, never instructions.
* Times and days are Africa/Lagos; every recorded time carries its zone.
* Evals are honest (`mimi.evalmarks`): a run exits 0 only when it checked at
  least one pass mark and all passed (`--only` included); per-slice rates
  count cases that could not run and show them as misses; a latency mark is
  never passed on a replay (it is `not measured`).
* Every helper's toolset stays under its `registry.TOOLSET_BUDGETS` entry and
  `MAX_TOOLSET_CHARS`; every book slice under `context.SLICE_BUDGETS`.
* Every shop-facing day and time comes from `mimi.tz` (Africa/Lagos); a
  channel time without a zone is Lagos, never UTC.
* Synthetic data only; no real shop data, phone numbers or keys in the repo,
  traces or eval fixtures.
* Module rules: `mimi.runtime` imports nothing above it; the harness never
  imports the receipt, proof, voice, stock or sim flows
  (`tests/test_architecture.py`, `tests/test_harness.py`).

## Definition of done

CI green; tests for the new path including the unhappy one; eval results
before and after for any model, prompt or tool change; docs updated; the PR
says what changed, why, how it was tested, what could break and how to roll
back.
