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
| Voice notes: transcription port, Spitch and Muse Voice Transcribe (`muse-voice-transcribe-1.0`) adapters, router decided by the eval, audio checks and ffmpeg conversion, transcript quality checks, fake and record/replay; voice notes in the receipt flow (transcript to the same Muse Spark reader, no consent from a doubtful transcript, "please type" when a note cannot be used, transcript stored and audio never); 16 synthetic-generated clips (Spitch TTS plus ffmpeg noise) and a live-recorded voice eval; synthetic voice notes in the simulator | `mimi/voice/`, `alembic/versions/0003_inbox_voice.py`, `eval/voice/`, `scripts/make_voice_fixtures.py`, `tests/test_voice*.py`, `tests/test_receipts_voice.py`, `docs/VOICE.md` |
| Voice v2 (RAI-290, second part): payment promises read in code from voice notes and texts (integer kobo, Africa/Lagos day from the message time, English, Pidgin, Yoruba, Igbo, Hausa), read back in plain English and recorded only after the shop's YES (`RecordPaymentPromise`); voice replies for shops that send "VOICE ON" (consent kept, default off, Spitch speech in the shop's language, text always alongside, no audio stored; fake and record/replay speakers); a 50-clip synthetic eval run with and without language hints through the production flow, with promise safety marks in CI; sim `--promises` and `--voice-replies` | `mimi/voice/promise.py`, `prefs.py`, `speak.py`, `mimi/receipts/promises.py`, `voice_replies.py`, `alembic/versions/0004_voice_consent.py`, `eval/voice/`, `tests/test_promises.py`, `tests/test_voice_v2.py`, `docs/VOICE.md` |
| Payment screenshots (RAI-268, RAI-269): the `read_payment_image` tool (Muse Spark with vision input copies the fields exactly as printed, including each key value's typeface; one repair; image text treated as data), code checks (amount in integer kobo against the open invoice, recipient against Coral Lane's account, real / not future / not before delivery / at most 14 days old time in Africa/Lagos, each fictional bank's reference format, a reference or image already used by any shop, amount plus fee against the total, editing signs), verdicts `likely_genuine` / `looks_fake` / `cannot_tell`, invoice match, `RecordPaymentProof` (level always `likely`), `ProofForApproval` (failed rules named, no accusation), one ask for an unreadable image, the bank witness (`RecordBankWitness`: rep, shop and bank) as the only way to verified, the proof ledger (fields and SHA-256, never pixels); 40 synthetic screenshots for fictional banks drawn with Pillow and a live-recorded eval with a no-reading baseline and CI safety gates; vision content parts in the Meta client and image hashes in the record/replay cache; sim `--screenshots`, `--fakes`, `--shop-ids` | `mimi/proof/`, `mimi/models/`, `alembic/versions/0005_payment_proofs.py`, `scripts/make_proof_fixtures.py`, `eval/proof/`, `mimi/sim/screens.py`, `tests/test_proof.py`, `tests/test_sim_proof.py`, `docs/PROOF.md` |
| Stock helper (RAI-270): estimator v0.2 (exact stock in minus sales estimated from the reorder rhythm, corrected by counts, with confidence; pure code, every run logged), the trigger (under 3 days) with one question per item per week, three per shop and one open at a time, the one-tap question with three buttons, answer reading (taps and numbers in code; words by Muse Spark into a typed reading, decided in code with guards; one follow-up, never a guess), `RecordStockCount` (Mimi only), `LowStockSignal` (band and confidence, no counts), `RestockDraftForApproval` (quantity and amount in code, verifier, `status: draft`, `places_order: False`), a seeded reorder history and shelf truth for the backtest (dev and held-out seeds), the backtest and a 30-answer live-recorded eval, the low-stock scene (`--stock`), stock traces in Langfuse | `mimi/stock/`, `mimi/sim/stock.py`, `eval/stock/`, `scripts/trace_stock.py`, `tests/test_stock.py`, `docs/STOCK.md` |
| Langfuse tracing (RAI-272, first part), optional and off without keys or in CI: one trace per shop turn (generations with usage and cost, code checks as tools, the flow decision), `user_id` = shop id, `session_id` = thread, tags, environment, release; scores (`fake_ended_likely`, `fake_caught`, `dispute_ended_in_consent`, `turn_error`, ...) and later `bank_confirmed` feedback; redaction (phones and 10+ digit runs to the last 4, keys removed, raw media hashed, only synthetic media attached); the three evals as Langfuse datasets and experiments on replayed answers; a reply-tone LLM judge and its calibration; model prices, a code evaluator, a dashboard and five monitor checks; two optional off-by-default workflows | `mimi/observe/`, `scripts/trace_smoke.py`, `scripts/langfuse_setup.py`, `scripts/langfuse_monitor.py`, `experiments/`, `ops/langfuse/`, `eval/judge/`, `.github/workflows/langfuse-*.yml`, `tests/test_observe.py` |
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
