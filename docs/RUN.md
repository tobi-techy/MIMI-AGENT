# Running Mimi's world, simulator and eval

All data is synthetic (see `WORLD.md`). Commands run from the repo root after
`pip install -e '.[dev]'` (Python 3.11 or 3.12). The `Makefile` has a target
for each of these.

## 1. Generate or check the world

```bash
# Write the fixture for the default seed (2610) and print its hash and stats
python -m mimi.world

# Another seed, somewhere else
python -m mimi.world --seed 7 --out /tmp/world-seed7.json

# CI-style check: stats reconcile and the committed fixture has not drifted
python -m mimi.world --check
```

## 2. Run the live day without a key (FakeModel, offline)

With `META_API_KEY` unset, Mimi's judge and the shop personas both use the
deterministic FakeModel. This is what CI runs.

```bash
python -m mimi.sim                  # all 40 shops, report only
python -m mimi.sim --transcript     # plus the WhatsApp transcript
python -m mimi.sim --shops 3 --transcript
```

## 3. Run with real Muse Spark calls

```bash
export META_API_KEY=...                              # Meta Model API key; never commit it
export META_API_BASE=https://api.meta.ai/v1          # default, can be omitted
export MIMI_MODEL=muse-spark-1.3                     # Mimi's reply judge (default)
export SIM_MODEL=muse-spark-1.3-contributor          # the shop personas (default)
# optional
export META_REASONING_EFFORT=low                     # minimal | low | medium | high
export META_TIMEOUT_S=60

python -m mimi.sim --shops 3 --transcript
```

The report prints prompt and completion tokens for both sides. Every money
figure Mimi sends is computed in code. Mimi's model reads each settled reply
burst in context (receipt lines, the shop's earlier messages) and returns a
typed verdict (confirmed / disputed / partial / unclear / unrelated, language,
confidence, evidence quote). Code guards decide what happens: consent only for
`confirmed` at confidence 0.8 or more with a quote that is really in the
shop's messages; disputes always go to the owner; anything else gets one
clarifying question and then an owner flag.

A long live run can be resumed: `--cache /tmp/sim3` records every model
answer to `/tmp/sim3.mimi.json` and `/tmp/sim3.sim.json` and replays them on
a rerun, so a cut-off run picks up where it stopped.

```bash
python -m mimi.sim --shops 10 --cache /tmp/sim10
```

## 3b. The reply-reader eval

```bash
python -m mimi.receipts.eval            # replay recorded answers (CI)
python -m mimi.receipts.eval --live     # call Muse Spark, resume-safe
python -m mimi.receipts.eval --live --fresh   # all calls new
python -m mimi.receipts.eval --fake     # keyword FakeModel baseline
```

Cases are in `eval/receipts/receipt_replies.jsonl` (synthetic, hand-written).
`--live` writes `receipt_replies.cache.json` (the recorded answers CI replays)
and `receipt_replies.report.json`. Changing the prompt or a case makes CI miss
the cache: re-record with `--live --fresh`.

## 3c. Voice notes

Speech to text for WhatsApp voice notes (`docs/VOICE.md`). Keys from the
shell, never written into a file in this repo:

```bash
export SPITCH_API_KEY=...       # Spitch (Yoruba, Igbo, Hausa, Pidgin, default)
export META_API_KEY=...         # also used by muse-voice-transcribe-1.0
# optional
export MIMI_VOICE=router        # router (default) | spitch | muse | fake
export MIMI_TTS=spitch          # voice replies: spitch (default with a key) | fake
export VOICE_TIMEOUT_S=30
```

With neither key set the transcriber is the deterministic fake: each
fixture clip transcribes to the text it was made from, and any other audio
fails (so the "please type your reply" path runs offline too). ffmpeg must
be installed for MP3 and M4A clips and for Muse Voice Transcribe, which only
takes 16 kHz mono WAV; Ogg/Opus and WAV durations are read without it.

```bash
python -m mimi.voice.eval            # replay recorded transcripts and verdicts (CI)
python -m mimi.voice.eval --live     # Spitch + Muse Voice Transcribe + Muse Spark
python -m mimi.voice.eval --live --fresh   # all calls new
python -m mimi.voice.eval --fake     # fake transcriber and keyword judge (plumbing)
python -m mimi.voice.eval --strict   # also fail on the first-read dispute mark
python -m mimi.voice.eval --hints on # only with the shop's language hint (default: both)
python -m mimi.voice.eval --live --only vn-03 --hints off   # record one missing call
```

Each clip runs with the shop's language as a hint and with none
(`spitch/no-hint`, ...), through the production `ReceiptFlow`; a payment
promise read back is answered by a truthful shop (YES if right). The run
exits 1 when a safety mark fails: a false confirm, a dispute that ends in
consent, or a promise recorded without the shop's YES or with the wrong
amount or day. Recorded answers: `eval/voice/transcripts.spitch.cache.json`,
`transcripts.muse.cache.json` (text only, never audio) and
`verdicts.cache.json`; `--live` writes `voice.report.json` (with the cost of
recording it). A live run is resumable: a call that errors is not recorded,
so rerunning `--live` makes only the missing calls.

Voice notes in the simulator: `--voice SHARE` makes that share of shops
answer the receipt with a synthetic clip in their language (a dispute clip
when goods were short), and turns every "[voice note]" placeholder into a
clip. The transcript prints what Mimi heard and which service heard it.

`--promises SHARE`: that share of those shops send a clip that also
promises to pay ("Everything complete. I go pay 20k tomorrow"); Mimi reads
the amount and day back and records the promise only after the persona's
YES. `--voice-replies`: every shop sends "VOICE ON" at the start of the day
(recorded as its consent), and Mimi's short replies also go as a voice note
(Spitch with `SPITCH_API_KEY`, else the fake). The transcript shows each
voice reply's length, language and voice; the audio is dropped.

```bash
python -m mimi.sim --shops 3 --transcript --voice 1
python -m mimi.sim --shops 10 --voice 0.3 --cache /tmp/sim10   # also records transcripts
# the live smoke: voice notes, a payment promise, voice replies
python -m mimi.sim --shops 3 --transcript --voice 1 --promises 1 --voice-replies \
  --cache /tmp/smoke/run      # resumable; voice replies recorded under /tmp/smoke/run.tts
```

Live smoke, 26 Oct 2026 (`muse-spark-1.3`, router, Spitch speech): 3 shops,
3 voice notes, 2 confirmed and 1 dispute flagged (0 confirmed despite a
shortage). The Pidgin-English shop's note "All goods received. I will pay
fifty thousand naira at the end of the month" was read back as "N50,000 on
Saturday 31 October 2026", the shop answered "YES correct, 50k for 31st",
and `RecordPaymentPromise` was recorded (5,000,000 kobo, 2026-10-31). The
Yoruba shop's note ran on in the speech engine and its amount could not be
read, so Mimi asked (in text, and as a 4.2 s Yoruba voice note) and recorded
nothing; that persona never answered. 2 voice replies, 13.7 s, about USD
0.03 of Spitch credit.

Regenerate the fixtures (costs Spitch credit; about 160 seconds of speech,
about USD 0.23):

```bash
python scripts/make_voice_fixtures.py          # missing clips only
python scripts/make_voice_fixtures.py --force
```

## 3d. Payment screenshots

```bash
# The 40-screenshot eval: replay (offline, what CI runs), live, oracle, baseline
python -m mimi.proof.eval
python -m mimi.proof.eval --live            # resumes from eval/proof/screens.cache.json
python -m mimi.proof.eval --live --fresh    # every case a new call (about 113k tokens)
python -m mimi.proof.eval --fake            # perfect reader: the code checks vs the labels
python -m mimi.proof.eval --baseline

# Redraw the 40 fixtures and labels (Pillow; deterministic for the seed, but
# JPEG bytes can change with the Pillow version, so re-record after)
python scripts/make_proof_fixtures.py

# Shops pay by screenshot in the sim: --screenshots SHARE of the transfer
# shops send one for the day's delivery, --fakes SHARE of those a fake one.
# The first three live shops pay cash, so name transfer shops with --shop-ids.
META_API_KEY= python -m mimi.sim --shop-ids shop-019,shop-037,shop-025 --transcript \
  --screenshots 1 --fakes 0.4
# the live smoke (Muse Spark reads the screenshots too)
python -m mimi.sim --shop-ids shop-019,shop-037,shop-025 --transcript \
  --screenshots 1 --fakes 0.4 --cache /tmp/smoke/proof
```

The transcript shows each screenshot (with the sim's truth for a fake), what
Muse Spark copied off it (`Mimi (reads screenshot)`), the code verdict and
failed rules (`Mimi (checks)`), Mimi's reply, and later the bank's
confirmation. The report adds `proof_verdicts`, `proofs_likely_recorded`,
`proofs_to_approvals`, `proof_approval_rules`, `proofs_verified_by_bank`,
`verified_without_bank_credit` (always 0), `fakes_marked_likely` and the
reader's tokens. Offline, the reader is a fake that looks up the renderer's
own record of each image (by SHA-256); it does not read pixels.

Live smoke, 27 Oct 2026 (`muse-spark-1.3` for Mimi and the screenshots,
`muse-spark-1.3-contributor` for the shops): 3 shops, 3 receipts confirmed,
4 screenshots. Moji Kiosk's Harmattan MFB receipt (N139,300) and Kunle POS &
Provisions' Ironleaf SMS alert (N13,600) were read exactly, matched their
invoices (`exact`), recorded as likely and confirmed by the sim's bank feed
within two hours (verified, three witnesses). Kunle's second send of the
same image got "I already have this screenshot", with no model call and
no second match. Titi POS & Provisions' fake (a Lagoonpay receipt to an
account ending 9917) went to approvals with `recipient` named (Muse Spark also
reported a typeface difference on it, so `editing_signs` failed too), and the shop got the neutral "sent to Coral Lane
Trading to confirm with the bank". 0 fakes marked likely, 0 verified without
a bank credit. Screenshot reading: 5,918 prompt and 3,159 completion tokens
for 3 reads; 9 live calls in all (with the receipt reader and the shops),
49 s.

## 3f. Stock: backtest, answer eval and the low-stock scene

```bash
# Backtest: estimator v0 with the one-tap loop, day by day, no model calls.
# Reorder history on the dev seed (tuned) and the held-out seed (the mark),
# plus the world's own deliveries as a stress test. Compares to the report.
python -m mimi.stock.backtest
python -m mimi.stock.backtest --write     # rewrite eval/stock/backtest.report.json
python -m mimi.stock.backtest --strict    # also fail under the proposed 70% bar

# The 30-answer reading eval: replay (offline, what CI runs), live, fake, baseline
python -m mimi.stock.eval
python -m mimi.stock.eval --live          # resumes from eval/stock/answers.cache.json
python -m mimi.stock.eval --live --fresh  # every case a new call (about 37k tokens)
python -m mimi.stock.eval --fake          # keyword FakeModel (plumbing)
python -m mimi.stock.eval --baseline      # code only: taps and bare numbers

# The low-stock scene: Mon 26 Oct 07:30 WAT stock check for shop-007,
# shop-004 and shop-002 (or --shop-ids / --shops N), one-tap questions, the
# shops' answers from their true shelf, reads, counts, restock drafts
META_API_KEY= python -m mimi.sim --stock --transcript
python -m mimi.sim --stock --transcript --cache /tmp/smoke/stock              # live
python -m mimi.sim --stock --stock-words --transcript --cache /tmp/smoke/stock # shops type
python -m mimi.sim --stock --stock-history route --shop-ids shop-001 --transcript
```

The scene's transcript shows Mimi's estimate for each item it asks about
(`Mimi (estimates)`: units in, sell rate, units left, days, confidence), the
question with its buttons, the shop's answer, how it was read
(`Mimi (reads)`: bucket, exact number, tap / typed / model, the guard's
reason) and the thanks. The report lists the drafts (quantity, amount in
kobo, `status: draft`, `places_order: false`), what the distributor sees
(band and confidence only) and the tokens. With Langfuse on,
`make trace-stock` runs it with `--stock-words` and prints a public link to
one draft trace. Live smoke numbers: [`docs/STOCK.md`](STOCK.md#the-low-stock-scene).

## 3e. Langfuse tracing and experiments (optional)

Off unless `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are both set, and
always off in CI (`CI` / `GITHUB_ACTIONS`). Keys come from your shell, never
from a file in the repo.

```bash
pip install -e '.[langfuse]'
export LANGFUSE_PUBLIC_KEY=...                      # never commit either key
export LANGFUSE_SECRET_KEY=...
export LANGFUSE_HOST=https://cloud.langfuse.com     # EU; US is https://us.cloud.langfuse.com
# optional
export LANGFUSE_TRACING_ENVIRONMENT=dev             # default dev; trace-smoke uses demo
export LANGFUSE_RELEASE=...                         # default: the git sha
export MIMI_TRACE_MEDIA=0                           # do not attach even synthetic media
```

**Traces.** Any sim run traces once the keys are set. `make trace-smoke`
runs the 3-shop screenshot day (shop-019, shop-037, shop-025: a genuine
screenshot, a fake the checks catch, a resend) in environment `demo`,
records the model answers in `.cache/trace-smoke.*.json` (git-ignored) so a
rerun costs nothing, and makes the first caught fake's trace public. Example:
https://cloud.langfuse.com/project/cmuqt408400qvad0cm8p0tgms/traces/9202edc3b9f02645a01a26081c22d3d0

One trace per shop turn: the turn root (`agent`), generations for each model
call (Muse Spark, Muse Voice Transcribe, Spitch) with usage and cost, a span
for the screenshot read, `decide-next-action` (chain) and each code check
(`check-*`, tool). `user_id` = shop id, `session_id` = shop plus the
delivery or payment thread, tags = feature, `verdict:*`, `language:*`,
distributor. Scores on the trace: `turn_error`, `shop_reply`,
`clarification_asked`, `fake_caught`, `fake_ended_likely`,
`dispute_ended_in_consent`, `promise_recorded`; `bank_confirmed` arrives
later as feedback when the bank feed confirms or misses a "likely" payment.

**Datasets and experiments.** The three evals run as Langfuse experiments on
the same production code as the offline evals, replaying the recorded
answers (a cache miss fails the item, so there is no new model spend).

```bash
make langfuse-datasets      # mimi/proof-screenshots (40), mimi/voice-notes (50), mimi/receipt-replies (84)
make proof-experiment       # add JUDGE=1 for the reply-tone judge scores
make voice-experiment
make receipts-experiment
make judge-calibration      # the reply-tone judge on 8 labelled replies (cached Muse answers)
```

Code evaluators: `verdict_correct`, `fake_caught`, `false_likely_genuine`,
`false_confirm`, `field_accuracy`, `tokens`, `latency_s`, plus run-level
rates. The judge adds `reply_respectful`, `reply_figures_grounded` and
`reply_plain_english`.

**Project setup and monitors.** `make langfuse-setup` (idempotent) adds the
model prices in `ops/langfuse/models/`, the code evaluator
`reply-accusation-free` with an enabled rule on turn roots, the LLM judge
`reply-tone` and its rule **disabled**, and a "Mimi monitors" dashboard.
`make langfuse-monitor` checks five things over the last 24 h from the
Metrics API and exits 1 on a breach: fake ended likely, bank rejected a
likely payment, dispute ended in consent (all critical), error rate over 5%,
generation p95 over 20 s, daily cost over USD 2. Two optional workflows,
both off by default: `.github/workflows/langfuse-monitor.yml` (hourly, needs
the repo variable `LANGFUSE_MONITOR=true` and the `LANGFUSE_*` secrets) and
`.github/workflows/langfuse-experiment.yml` (manual only, needs
`LANGFUSE_EXPERIMENTS=true`; pull requests never trigger it).

**Two manual steps in the Langfuse UI:**

1. **LLM connection for the reply-tone judge.** Project settings, LLM
   connections: add one, then enable the `reply-tone` evaluator rule that
   `make langfuse-setup` left disabled (after checking the judge calibration
   run).
2. **Two critical alerts.** The Hobby plan allows two alerts per
   organisation and they can only be made in the UI: one on any
   `fake_ended_likely` score true, one on any `dispute_ended_in_consent`
   score true. The other checks run in `make langfuse-monitor`.

## 4. Clock modes and persistence

```bash
# Fast-forward (default): the sim clock jumps from event to event
python -m mimi.sim --hours 26

# Real time for a demo: 1 = wall clock, 60 = one sim hour per minute
python -m mimi.sim --shops 3 --realtime 60 --transcript

# Persist jobs, shop replies, voice-reply consents and payment proofs (SQLite creates the tables)
python -m mimi.sim --db sqlite:////tmp/mimi-sim.db

# Postgres: install the extra, then create the tables with alembic first
# (revisions 0001 to 0005). MIMI_DB_URL is a SQLAlchemy URL for your own
# local database, set in your shell (never written into a file in this repo).
# Revision 0003 adds the voice-note columns (transcript, never audio);
# 0004 the shops' voice-reply choices, each with its consent message;
# 0005 payment screenshots as fields (SHA-256 and size, never the image).
pip install -e '.[postgres]'
alembic upgrade head
python -m mimi.sim --db "$MIMI_DB_URL"
```

Jobs land in `runtime_jobs` (the durable state machine, readable by the
console), shop replies in `shop_inbox`, voice-reply choices in
`shop_voice_consent` and payment screenshots, as fields, in `payment_proofs`.

## 5. Tests

```bash
META_API_KEY= pytest          # the whole suite, offline
make check                    # lint, types, tests, the five evals, world, secrets, provenance
```

The tracing tests (`tests/test_observe.py`) use a recording exporter and a
mock Langfuse client behind a socket guard, so they never reach the network.
