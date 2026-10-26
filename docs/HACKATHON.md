# Meta Global AI Developer Hackathon: what is in this repository

Build window: 26 Oct 2026 12:00 PM ET (17:00 WAT) to 4 Nov 2026 9:00 PM ET
(5 Nov 03:00 WAT). Theme: Build for Your World. Track: Startup.

**Organisers' ruling, 26 Oct 2026 (Cerebral Valley):** only work done during
the hackathon goes in a fresh public repo, kept separate from pre-existing
work. This is that repo. It was created on 26 Oct 2026 inside the window, and
every commit in it was made inside the window
(`python scripts/hackathon_provenance.py` checks this in CI).

## Built during the window (everything in this repo)

| Component | Where |
|---|---|
| Shared types: `ShopScope` (tenant and shop on every intent) and the kobo money unit, written fresh for this repo | `mimi/types.py` |
| Always-on runtime: durable job state machine, leases with heartbeat and takeover, outbox with idempotency keys, step/time/naira budgets, owner preemption at step boundaries, per-step tracing; helper-registered intent types (money verbs refused); `JobStore.wake` for an outside event such as a shop reply | `mimi/runtime/`, `alembic/versions/0001_runtime_jobs.py`, `tests/test_runtime_jobs.py`, `docs/RUNTIME.md` |
| Synthetic world: Coral Lane Trading (fictional), 40 fictional shops, 30 invented-brand SKUs, 8 weeks of deliveries, payments, bank feed, promises, disputes and 4 fake transfer alerts; fixed seed, JSON fixture, loader, stats check | `mimi/world/`, `tests/test_world.py`, `docs/WORLD.md` |
| Model adapter: client for Meta's Model API (env-configured, timeout, one retry, JSON schema response format) and a deterministic FakeModel; record/replay cache | `mimi/models/`, `tests/test_models.py` |
| Delivery receipt flow: receipt built by code, "Reply YES to confirm", consent with a shop witness, one clarification, owner flags; typed intents; front door with dedupe and burst settling; persisted shop inbox | `mimi/receipts/`, `alembic/versions/0002_shop_inbox.py`, `tests/test_receipts.py` |
| Reply reader: Muse Spark reads the whole reply burst in context and returns a typed verdict (outcome, disputed items, language, confidence, evidence quote, reason); one repair try; code guards (consent only at confidence 0.8 or more with a quote really in the shop's messages; disputes always flagged; injection text never confirms) | `mimi/receipts/verdict.py`, `decide.py`, `tests/test_verdict.py` |
| Reply eval: 84 synthetic hand-written cases, recorded live on Muse Spark and replayed in CI | `eval/receipts/`, `mimi/receipts/eval.py` |
| Shop simulator: model-driven shop personas in per-shop voices, sim clock (fast-forward or real time), a stand-in for the system of record, resumable live runs | `mimi/sim/`, `docs/RUN.md` |
| Packaging, alembic env, Makefile, CI (tests on Python 3.11 and 3.12, detect-secrets, gitleaks, provenance), module rules test | `pyproject.toml`, `alembic/`, `Makefile`, `.github/workflows/ci.yml`, `tests/test_architecture.py` |
| Compliance notes and dependency list | `docs/COMPLIANCE.md`, `docs/DEPENDENCIES.md` |

The code was first written in a private staging branch during the window and
then ported here. The port brought over only code written in the window. The
one thing it needed from earlier code (a two-field scope type and the intent
type list) was written fresh in `mimi/types.py`; the runtime now starts with
no intent types of its own and each helper registers its own.

## Built before the event (not in this repo)

* The marketing website, userail.money.
* A console UI prototype. Any console screen shown in the demo is labelled
  pre-existing.
* An earlier Mimi backend (a shop document reader, a ledger service and trust
  checks), in a separate private repository. Nothing in this repo imports or
  calls it; the simulator's `SimGo` stands in for the ledger.

## Data

All demo data is synthetic; Coral Lane Trading is fictional. The model calls
are real.
