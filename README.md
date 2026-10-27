# Mimi Agent

Mimi is a set of WhatsApp agents for FMCG distributors in Nigeria. A
distributor's van drops goods at dozens of small shops a day, and most
arguments about money start with "that is not what you delivered". Mimi sends
each shop a receipt on WhatsApp right after the drop, reads the reply in
whatever language the shop writes or speaks (English, Pidgin, Yoruba, Igbo,
Hausa or a mix, typed or as a voice note), records the shop's confirmation,
and flags any dispute to the owner with the shop's own words. When a shop
promises to pay ("I go pay 20k tomorrow"), Mimi reads the amount and the day
back and notes the promise only after the shop says YES. When a shop sends a
screenshot of a bank transfer as proof it paid, Mimi reads it, checks it in
code, matches it to the open invoice and marks it **likely** paid until the
distributor's own bank feed shows the money; one that looks wrong goes to the
owner, without a word of blame to the shop.

This demo does that end to end on a synthetic distributor: it confirms
deliveries with shops in any language and flags disputes. The money maths is
integer kobo, done by code. The model reads and judges; it never writes a
figure, and Mimi never moves money.

* Hackathon: Meta Global AI Developer Hackathon, 26 Oct to 5 Nov 2026
* Theme: **Build for Your World**
* Track: **Startup**

All demo data is synthetic; Coral Lane Trading is fictional. So is every shop,
person, brand, phone number and account in it. The model calls are real.

## Meta models

| Model | Role |
|---|---|
| `muse-spark-1.3` | Mimi's reasoning: reads each shop's reply burst in context and returns a typed verdict (confirmed, disputed, partial, unclear, unrelated) with the language, a confidence and the exact quote it relied on. With vision input, it also reads payment screenshots into typed fields |
| `muse-spark-1.3-contributor` | The shop simulator: each fictional shop replies in its own voice |
| `muse-voice-transcribe-1.0` | Speech to text for WhatsApp voice notes: first choice for English notes, fallback for the rest |
| `sam-3.1` (planned) | Shelf photos, to check stock against what was delivered |

Third-party speech service:

| Service | Role |
|---|---|
| Spitch speech to text (`api.spitch.app`) | Speech to text for voice notes in Yoruba, Igbo, Hausa and Pidgin, and the default when the language is unknown. Paid per second of audio; called with data retention off |
| Spitch text to speech | Mimi's short voice replies to shops that turned them on, and the synthetic voice-note fixtures in `eval/voice/clips/` |

The speech services only turn audio into text. Muse Spark still does all the
reasoning: a voice note's transcript goes to the same reader and the same
code guards as a typed reply.

Code guards sit around the model. A delivery is recorded as confirmed only
when the verdict is `confirmed`, the confidence is at least 0.8 and the quote
really appears in the shop's own messages. Every dispute goes to the owner.
Text that tries to instruct Mimi never confirms anything. A voice note whose
transcript is doubtful, or could not be made, never confirms anything either:
Mimi asks the shop to type its reply.

Payment promises are read in code, never by a model: the amount in integer
kobo and the day in Africa/Lagos from the message's own time. Mimi reads
them back in plain English ("You said you will pay N20,000 on Tuesday 27
October 2026 ... Reply YES if that is right") and records the promise only
after the shop's YES. An unclear amount or day, or a doubtful transcript,
gets one question and records nothing. Mimi never moves money.

Payment screenshots: Muse Spark copies the amount, sender, recipient, bank,
date and time, reference and status off the image exactly as printed (and
the typeface of each key value). Code turns the amount into integer kobo and
the time into Africa/Lagos and checks it: the amount against the open
invoice, the recipient against Coral Lane's account, a time that is real,
not in the future, not before the delivery and not weeks old, the bank's
reference format, a reference or image already used by any shop, amount plus
fee against the total, and any editing the model points at. The verdict is
`likely_genuine`, `looks_fake` or `cannot_tell` (one clear ask for a clearer
screenshot). A screenshot never makes a payment verified: only the bank's
credit does, when the rep's delivery, the shop's proof and the bank agree.
See [`docs/PROOF.md`](docs/PROOF.md).

Voice replies are off unless the shop itself sends `VOICE ON`; that message
is kept as its consent. Then Mimi's short replies also go as a voice note in
the shop's language (Spitch speech; Pidgin through an English-language
voice). The text always goes with it, and every amount and day is in the
text. No audio is stored.

## Quickstart

Python 3.11 or 3.12.

```bash
git clone https://github.com/tobi-techy/MIMI-AGENT.git
cd MIMI-AGENT
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
```

**Offline** (no key, deterministic FakeModel, recorded Muse Spark answers):

```bash
META_API_KEY= pytest                                   # full test suite
META_API_KEY= python -m mimi.receipts.eval             # eval, replaying recorded answers
META_API_KEY= python -m mimi.sim --shops 3 --transcript   # one simulated day, 3 shops
META_API_KEY= python -m mimi.voice.eval                 # voice eval, replaying recorded answers
META_API_KEY= python -m mimi.sim --shops 3 --transcript --voice 1   # shops send voice notes
META_API_KEY= SPITCH_API_KEY= python -m mimi.sim --shops 3 --transcript --voice 1 --promises 1 --voice-replies
META_API_KEY= python -m mimi.proof.eval                 # screenshot eval, replaying recorded answers
META_API_KEY= python -m mimi.sim --shop-ids shop-019,shop-037,shop-025 --transcript --screenshots 1 --fakes 0.4
```

**Live** (real Muse Spark calls):

```bash
export META_API_KEY=...        # your Meta Model API key; never commit it
export SPITCH_API_KEY=...      # Spitch key, for voice notes; never commit it
python -m mimi.sim --shops 3 --transcript
python -m mimi.receipts.eval --live
python -m mimi.voice.eval --live
python -m mimi.sim --shops 3 --transcript --voice 1 --promises 1 --voice-replies
python -m mimi.proof.eval --live
python -m mimi.sim --shop-ids shop-019,shop-037,shop-025 --transcript --screenshots 1 --fakes 0.4
```

A live 3-shop day prints the WhatsApp transcript (receipts, shop replies,
Mimi's verdicts) and a report with token counts. More options, including
real-time clock mode, Postgres persistence and resumable long runs, are in
[`docs/RUN.md`](docs/RUN.md). The `Makefile` has shortcuts (`make test`,
`make eval`, `make sim`, `make check`).

## Eval: reading shop replies

84 synthetic, hand-written reply cases: plain English, Pidgin, Yoruba, Igbo,
Hausa, mixed, emoji only, "ok", late replies, partial deliveries, sarcasm,
voice-note placeholders (a note with no transcript), typos and prompt
injection. Recorded live on Muse
Spark 1.3 and replayed in CI.

| Reader | False confirms | Confirmed recall | Disputed recall | Exact label |
|---|---|---|---|---|
| Mimi on `muse-spark-1.3` | **0** | **100%** | **100%** | **94%** (79 of 84) |
| Keyword baseline (FakeModel) | 2 | 44% | 48% | 63% |

A false confirm (recording "received in full" when the shop did not say so)
is the error that costs a distributor money, so the pass mark for it is zero.
The other five are accepted alternatives, all on the safe side: two
emoji-only replies read as `unclear` (Mimi asks once), two partial deliveries
read as `disputed` (flagged to the owner), and one off-topic message read as
`unclear`. None is a confirm. Cases:
`eval/receipts/receipt_replies.jsonl`; full report:
`eval/receipts/receipt_replies.report.json`.

## Eval: voice notes

50 synthetic voice notes (Spitch text-to-speech voices plus ffmpeg market
noise; no real person's voice) in English, Pidgin, Yoruba, Igbo, Hausa and
code-switched mixes: confirms, disputes ("na 4 carton, no be 6"), partial
deliveries, payment promises with amounts and days, unclear and unrelated
replies. Each runs through each transcriber with the shop's language as a
hint and with none, then end to end through the production flow (Muse Spark
verdict, guards, promise read-back). Recorded live and replayed in CI.

| Transcriber | WER | Verdicts right | False confirms | Disputes flagged first read | Promise amount and day right |
|---|---|---|---|---|---|
| Router (production) | **22%** | **46 of 50** | **0** | 13 of 15 | 13 of 14 |
| Spitch only | 31% | 45 of 50 | 0 | 12 of 15 | 13 of 14 |
| `muse-voice-transcribe-1.0` only | 41% | 38 of 50 | 0 | 10 of 15 | 10 of 14 |
| Router, no language hint | 28% | 43 of 50 | 0 | 11 of 15 | 12 of 14 |

For every transcriber, with and without a hint: no dispute ever ends in
consent, and no promise is recorded without the shop's YES or with a wrong
amount or day (these marks fail CI). The router's 10 promise read-backs were
all right and all recorded correctly after the YES; the one promise it
missed is a clip whose amount could not be heard, so Mimi asks. The two
disputes it missed on the first read are read as unclear, so Mimi asks once
and then flags the owner. Recording the eval cost about USD 0.40 of Spitch
credit. Per-language table, API shapes, the promise rules and the router
policy: [`docs/VOICE.md`](docs/VOICE.md).

## Eval: payment screenshots

40 synthetic screenshots (fictional banks and logos, drawn with Pillow): 20
genuine, 16 fakes (edited amount, wrong recipient, duplicated reference,
impossible time, "Pending" shown as success, mismatched fonts, an old
screenshot reused) and 4 that cannot be read, with JPEG compression, crops,
status bars, rotation and blur. Each runs through the production desk and
flow. Recorded live on Muse Spark 1.3 and replayed in CI.

| Reader | Field exact match | Fakes caught | False "likely genuine" on fakes | Real marked fake | Invoice match | Verified from a screenshot |
|---|---|---|---|---|---|---|
| Muse Spark + code checks | **100%** | **16 of 16** | **0** | **0** | **20 of 20** | **0** |
| No-reading baseline | n/a | 0 of 16 | 16 | 0 | n/a | n/a |

All 4 unreadable ones get one clear ask. About 2,800 tokens and 5 s (p95
8.3 s) per screenshot. Two fakes are caught only by what the model sees
(values typed over in another font); the rest by code on the copied fields.
These are synthetic renders, so real screenshots will be harder; a forgery
that keeps every printed figure consistent is caught by the bank witness,
which is why a screenshot alone only ever means "likely". Failures, fixes
and limits: [`docs/PROOF.md`](docs/PROOF.md).

## The agent harness

Every helper runs on one in-house loop (`mimi/harness/`): think, call typed
tools, observe, repeat, until done, parked for the owner or out of budget.
Model-agnostic OpenAI-style tool calling (Muse Spark 1.3 here, with a
fallback model on a provider error). Budgets per run (12 steps, 60 s, N50 of
model spend, with the next call's worst case reserved), a daily naira cap per
distributor and kill switches are checked in code before every step. Tools
are declared with typed inputs and outputs, a consent scope, a risk tier read
from policy (never from the model), idempotency fields and a dry run; write
tools only return intents, high-risk ones wait in the owner's approvals, and
moving money or changing bank details cannot be put in any helper's toolset.
Replying is a tool: the model's own text is private.

Collector, the first hire, has its 13 tools. On 50 synthetic Collector
scenarios (chase everyone over 50k, balance asks, payments with and without a
bank credit, promises, quiet routines, disputes):

| | Task completion | Side effects from inside the loop | Over budget without stopping | Median model calls per turn |
|---|---|---|---|---|
| Live `muse-spark-1.3` (recorded, replayed in CI) | **50 of 50** | 0 | 0 | 2 |
| Scripted policy with injected faults (5xx, bad arguments, tier spoofing) | **50 of 50** | 0 | 0 | 2 |

Security suite: 41 of 41 refused (cross-distributor reads, missing consent,
tool names injected by shop text, tier spoofing, never-tier tools). 200
replayed retries and crashes: 0 duplicate side effects. An idle distributor
makes no model calls for 24 hours apart from its routine. Loop overhead is
about 13 ms at p95. Details: [`docs/HARNESS.md`](docs/HARNESS.md).

```bash
python -m mimi.harness.eval                         # every harness suite, offline (CI)
python -m mimi.harness.eval --live --record-only    # re-record Muse Spark on the 50 scenarios
```

### Router and context builder

Mimi is Chief of Staff: every wake-up comes through one front door (dedupe on
the provider's message id, one turn per thread, a burst of messages is one
turn, the owner's messages and "stop" on a fast lane) and the router picks
the helper and skill: rules first, then multilingual phrase rules (English,
Pidgin, Yoruba, Hausa, Igbo), then a small classifier distilled from Muse
Spark, then Muse Spark itself only when those cannot settle it. Unsure means
one plain question, never a guess; text inside a shop's message can never
steer the route. The context builder gives each run only what its skill
needs: a pinned, cache-friendly prefix (rules + one skill, tools, memory,
routines, environment), the thread so far, and the shop's consent-checked
slice of the book on the newest message, with a manifest on every trace.

| Router, 300 labelled messages | Accuracy | Clarify | Money misroutes | p95 latency |
|---|---|---|---|---|
| Pass mark | >= 95% | <= 8% | 0 | < 800 ms |
| Rules -> phrases -> fast classifier -> Muse Spark | **98.7%** | 4.3% | **0** | 4 ms (Muse Spark on 5% of messages) |
| Muse Spark for every text | 97.0% | 6.0% | 0 | 1,737 ms |

| Context builder, 50 Collector scenarios on live Muse Spark | Task success | Median input tokens per run | Cached input tokens |
|---|---|---|---|
| Full-history baseline | 46/50 | 54,432 | 34% |
| **Builder** | **50/50** | **10,179 (-81%)** | **81%** |

0 cross-distributor leaks with two distributors sharing a shop. Details:
[`docs/HARNESS.md`](docs/HARNESS.md).

```bash
python -m mimi.harness.router_eval      # router eval, recorded Muse Spark answers (CI)
python -m mimi.harness.context_eval     # context eval, both arms recorded (CI)
```

## Tracing and experiments with Langfuse (optional)

Langfuse is optional and **off unless both `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY` are set**. Without them (and always in CI) the
exporter is a no-op: no client, no thread, no network call. The offline
replay evals stay the CI gate.

With it on, every shop turn becomes one trace (`handle-receipt-reply`,
`handle-voice-note`, `check-payment-screenshot`, ...) with the model calls as
generations (tokens, latency, cost), each code check as a tool step and the
flow's decision as a chain step. `user_id` is the shop id (pseudonymous),
`session_id` the shop's delivery or payment thread, tags carry feature,
verdict, language and distributor, and every trace has an environment and a
release (git sha). Scores: `turn_error`, `shop_reply`, `clarification_asked`,
`fake_caught`, `fake_ended_likely`, `dispute_ended_in_consent`,
`promise_recorded`, and later `bank_confirmed` when the bank feed answers.
Phone numbers, account numbers and keys are redacted before anything is
sent, and only synthetic media is ever attached
([`docs/COMPLIANCE.md`](docs/COMPLIANCE.md)).

```bash
pip install -e '.[langfuse]'      # langfuse==5.0.4, pinned
export LANGFUSE_PUBLIC_KEY=...    # from your Langfuse project; never commit either
export LANGFUSE_SECRET_KEY=...
export LANGFUSE_HOST=https://cloud.langfuse.com   # EU (default)

make trace-smoke          # 3-shop screenshot sim traced (env demo); prints a public trace link
make langfuse-datasets    # upload the three evals as datasets (synthetic media included)
make proof-experiment     # proof eval as an experiment on recorded answers (JUDGE=1 adds the reply judge)
make voice-experiment
make receipts-experiment
make judge-calibration    # calibrate the reply-tone judge on 8 labelled replies
make langfuse-setup       # model prices, code evaluator, judge (disabled), dashboard
make langfuse-monitor     # the five monitor checks over the last 24 h
```

Experiments replay the recorded model answers, so they cost no new model
calls. Two steps are manual, in the Langfuse UI: add an LLM connection to
switch on the reply-tone judge, and create the two critical alerts (the
Hobby plan has no alerts API). Details: [`docs/RUN.md`](docs/RUN.md#3e-langfuse-tracing-and-experiments-optional).

## What is in this repo

| Package | What it does |
|---|---|
| `mimi/harness` | The agent loop (step, time and naira budgets, daily cap, kill switches, one repair then fail safe, model fallback, ordered events), the typed tool registry (scope, Go-policy risk tier, idempotency keys, dry runs), Collector's 13 tools, durable steps, Langfuse traces, the harness evals; the front door and router (RAI-280) and the context builder (RAI-281) with their evals |
| `mimi/runtime` | Always-on durable jobs: state machine, leases with heartbeat and takeover after a crash, outbox with idempotency keys so nothing is sent twice, step, time and naira budgets, owner messages taken at the next step |
| `mimi/world` | The synthetic distributor: 40 shops, 30 invented SKUs, 8 weeks of deliveries, payments, bank feed and fake transfer alerts, from a fixed seed |
| `mimi/models` | Meta Model API client, FakeModel, record and replay cache |
| `mimi/receipts` | The delivery receipt flow, the reply reader and its guards, payment-promise read-back, voice-reply scripts, the eval |
| `mimi/voice` | Voice notes: a transcription port with Spitch and Muse Voice Transcribe adapters, a router, audio checks and ffmpeg conversion, money and date reading, voice-reply consent and text to speech, fakes, record and replay, the voice eval |
| `mimi/proof` | Payment screenshots: the `read_payment_image` tool (Muse Spark vision), code checks, verdicts and invoice match, the bank witness, the proof ledger, the synthetic screenshot renderer, the eval |
| `mimi/observe` | Optional Langfuse tracing: turn traces, redaction, the evals as datasets and experiments (off without keys) |
| `mimi/sim` | Shop personas and a one-day simulator (shops can pay by screenshot) |
| `mimi/types.py` | `ShopScope` and the kobo money unit |

Docs: [`docs/HACKATHON.md`](docs/HACKATHON.md) (what was built when),
[`docs/RUNTIME.md`](docs/RUNTIME.md), [`docs/HARNESS.md`](docs/HARNESS.md),
[`docs/WORLD.md`](docs/WORLD.md),
[`docs/RUN.md`](docs/RUN.md), [`docs/VOICE.md`](docs/VOICE.md),
[`docs/PROOF.md`](docs/PROOF.md),
[`docs/COMPLIANCE.md`](docs/COMPLIANCE.md),
[`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md).

## Built before the event (not in this repo)

* The marketing website, userail.money.
* A console UI prototype.
* An earlier Mimi backend, in a separate repository.

None of these is in this repository, and nothing here imports or calls them.
Every commit here was made during the hackathon window;
`python scripts/hackathon_provenance.py` checks it in CI.

## Licence

Copyright (c) 2026 Rail Technology Concepts Ltd. All rights reserved. The
source is published for hackathon judging; see [`LICENSE`](LICENSE).
