# Architecture: the modules, and the rules the build enforces

This is the map a new engineer and a new agent read before adding code. It is
derived from the code on `main`, not from aspiration: where the code breaks the
intent, it says so in [Where reality breaks the layering](#where-reality-breaks-the-layering).

The rules below are checked in CI by `tests/test_architecture.py`. A crossing
fails the build with the rule name and the fix, so boundaries do not depend on
review discipline.

Shape: **one deployable, enforced module boundaries, plus separate worker
processes.** Not microservices. The API process serves HTTP; worker processes
run turns off the same store. See `docs/adr/0001-modular-monolith-and-workers.md`.

---

## The layers, bottom up

```
  mimi.api                         edge: HTTP routes + worker entrypoint (FastAPI)
      |
  flows (helpers)                  receipts proof voice stock credit orders
      |                            returns checkin brand computer sim
  agent spine                      harness  connectors  skills
      |                            (observe sits beside the runtime)
  data + durable                   store  world  models   runtime
      |
  foundation leaves                types tz clock owed flags provenance evalmarks
```

A module may import **downward** (toward the leaves) and **sideways within its
own layer**. It may not import upward. The API is the only edge; nothing imports
the API.

---

## Package map

`May import` lists what is legitimate; `Never` lists what the build refuses.
Everything also may import the standard library and third-party packages (the
foundation leaves use almost none).

| Package | What it is | May import | Never |
|---|---|---|---|
| `mimi/api` | The HTTP API and the worker entrypoint: turns, threads, shops, approvals, credit, connectors, policy; bearer keys per distributor; the durable agent composed over the store | anything in `mimi` | may not be imported by anything else in `mimi` |
| `mimi/harness` | The agent spine: one loop every helper runs on, the typed tool registry and policy tiers, the router, the context builder, claims verification, loop safety | data, runtime, leaves, `observe`, and the tool integrations `brand` + `computer` | the flows (`receipts`, `proof`, `voice`, `stock`, `sim`, `credit`, `orders`, `returns`, `checkin`) |
| `mimi/connectors` | The shop-tool transport behind Mimi's interface (Composio): read, propose, apply, undo, consent | `store`, `world`, `models`, runtime, leaves | `harness`, `api`, any flow |
| `mimi/skills` | The skills registry and driver (folder-based, hash-locked) that binds skill scripts to the spine | `harness`, data, runtime, leaves, and the flows it registers | `api` |
| `mimi/observe` | Optional Langfuse tracing and the evals as datasets/experiments (off without keys) | `runtime`, `models`, `types` | any flow (except its `experiments.py` driver) |
| `mimi/store` | The durable store: tables and migrations, `SqlGo` system-of-record ports, the turn queue, the seed | `harness`, `world`, `models`, runtime, leaves | `api`; any flow (see the known violation) |
| `mimi/world` | The deterministic synthetic distributor (Coral Lane) and its fixtures | leaves | any flow (see the known violation) |
| `mimi/models` | The Meta Model API client, `FakeModel`, the record/replay cache | leaves | any flow |
| `mimi/runtime` | Always-on durable jobs: state machine, leases with takeover, outbox, step/time/naira budgets, owner steer | `runtime`, `types`, `tz` | everything else |
| `mimi/receipts` | Delivery receipt flow, reply reader and guards, payment-promise read-back | spine, data, runtime, leaves; `voice` | `api` |
| `mimi/voice` | Voice notes: transcription port (Spitch, Muse Voice), audio checks, money/date reading, TTS | spine, data, runtime, leaves | `receipts`, `sim` (except its eval driver) |
| `mimi/proof` | Payment screenshots: vision read, code checks, verdicts, bank witness, ledger | spine, data, runtime, leaves | `sim` |
| `mimi/stock` | Stock estimator, one-tap count, restock draft, backtest | spine, data, runtime, leaves | `sim`, `receipts`, `proof`, `voice` |
| `mimi/credit` | Credit review, exposure, shelf count into a limit change | spine, data, runtime, leaves, `owed` | `api` |
| `mimi/orders` | Order intake and reading | spine, data, runtime, leaves | `api` |
| `mimi/returns` | The returns ledger (pure code, no model) | spine, data, runtime, leaves | `api` |
| `mimi/checkin` | The weekly one-tap stock check-in | spine, data, runtime, leaves | `api` |
| `mimi/brand` | Brand promo-check helper; reads promos, drafts, never sends | spine, data, runtime, leaves | `api` |
| `mimi/computer` | Computer tasks: a real browser in a container under code guardrails | spine, data, runtime, leaves | `api` |
| `mimi/sim` | The shop simulator and the low-stock scene: the one driver that wires every flow together | anything in `mimi` | `api` |

Root modules (`mimi/*.py`):

| Module | What it is | May import |
|---|---|---|
| `mimi/types.py` | `ShopScope` and the kobo money unit, shared by every part | nothing in `mimi` |
| `mimi/clock.py` | The one wall clock every stored timestamp reads | nothing in `mimi` |
| `mimi/tz.py` | The one Africa/Lagos timezone source for shop-facing days and times | nothing in `mimi` |
| `mimi/owed.py` | Overdue summed once in code, so every surface shows one figure | nothing in `mimi` |
| `mimi/flags.py` | Fixes that change what a recorded model call sees, behind one switch | nothing in `mimi` |
| `mimi/provenance.py` | Proof levels (verified, likely, estimated) as a stdlib-only leaf | nothing in `mimi` |
| `mimi/evalmarks.py` | Shared rules for how an eval reports and exits | nothing in `mimi` |

The foundation leaves import **nothing** in `mimi`, so any layer can depend on
them without a cycle.

---

## Where a new feature goes

1. **A new typed tool the model can call** belongs in the helper's flow package
   (`mimi/<helper>/tools.py`), registered in the harness registry with a policy
   tier and a dry run. Not in `mimi/harness` unless it is Collector's own.
2. **A new helper** is a new flow package with `tools.py`, `flow.py`, `eval.py`,
   following an existing helper (`mimi/stock`, `mimi/credit`). Wire it through
   `mimi/skills` and the router, not by editing the spine.
3. **A new outside system** (a shop tool, a bank, a channel) is a
   `mimi/connectors` transport or a flow adapter; never a new import in the
   harness.
4. **A shared pure helper** (a money sum, a date rule, a name parse) goes in a
   foundation leaf if it is truly cross-cutting (`provenance`, `owed`, `tz`) or
   in the flow that owns it; do not reach into another flow for it.
5. **A new table or port** goes in `mimi/store`; a new durable job shape goes in
   `mimi/runtime`. A new route goes in `mimi/api`.
6. **A new package** must be added to `PACKAGES` in `tests/test_architecture.py`
   and given a rule; `test_every_package_is_covered_by_a_rule` fails until you
   do, so no package lands without a decided layer.

---

## Where reality breaks the layering

Two crossings exist today. The build records them in
`KNOWN_LAYER_VIOLATIONS` in `tests/test_architecture.py` (so a *new* crossing
still fails) and they are listed here rather than fixed, because the fix
touches an area that other work is actively changing.

| Crossing | Where | Why it is wrong | Suggested fix (owner's call) |
|---|---|---|---|
| `world` -> `stock` | `mimi/world/expiry.py` imports `mimi.stock.estimator` and `mimi.stock.history` to build the slow-stock fixture | the world is the deterministic data layer; the estimator is a flow | move the fixture builder into `mimi/stock` (which owns `slow_eval`), or pass precomputed histories in |
| `store` -> `credit` | `mimi/store/book.py` imports `mimi.credit.looks` (`item_name`, `pieces_per_unit`) | the system of record should not depend on a flow | move the two pure name helpers into a foundation leaf (or `mimi/types`), where both can read them |

Deliberate edges that are **not** violations, for the record:

* `harness` -> `brand` and `harness` -> `computer`: the spine integrates those
  two tool packages on purpose (`mimi/harness/plain.py`,
  `mimi/harness/computer.py`), so they are not in the harness's forbidden set.
* Eval and driver files (`*/eval.py`, `*_eval.py`, `observe/experiments.py`,
  `harness/review_pocs.py`) and `mimi/sim/__main__.py` wire every flow together
  to test them; the rules skip them, as the previous architecture test already
  did for `voice/eval.py`.
* `mimi/skills/refs.py` and `mimi/skills/evals.py` read flows to register and
  evaluate skills; that is the skills driver's job.

Report any crossing you find to the architecture owner rather than restructuring
someone else's package.

---

## How the rules are enforced

`tests/test_architecture.py` builds a cross-package import graph with the AST
(no runtime import, so it sees the whole tree offline) and applies the rules
above. A violation prints the rule name, the offending file and the fix. The
same file proves the guardrail works: `test_the_checker_catches_a_bad_import`
feeds the checker a deliberately bad `runtime -> harness` import and asserts it
trips with the fix message. `test_every_package_is_covered_by_a_rule` makes a
new package decide its layer.

We use a hand-rolled AST check rather than `import-linter` because it adds no
dependency (so CI installs nothing new) and because the failure message and the
eval-driver exemption are exactly ours to control. If the rule set outgrows it,
`import-linter` is the migration target.

New package checklist:

1. Put the package under `mimi/` with a one-line `__init__` docstring saying
   what it is.
2. Add it to `PACKAGES` in `tests/test_architecture.py` and give it a `Rule`
   (or fold it into an existing layer's rule).
3. Add a row to the package map above.
4. Run `make test` (or `pytest tests/test_architecture.py`) before pushing.

---

## The deployable

One image (`Dockerfile`) runs `mimi.api serve`; `mimi.api worker` runs the same
code as a separate process for turns. The queue lives in the database the store
already uses, claimed with leases so a killed worker loses no turn. Postgres is
the system of record in production (`docs/adr/0002-postgres-system-of-record.md`).
No microservices, no message broker of our own; see
`docs/adr/0005-pilot-scale-calibration.md` for the scale track and its triggers.
For the runtime flow (front door, router, helpers, loop, claims, store), the
diagram is `docs/architecture.mmd`.
