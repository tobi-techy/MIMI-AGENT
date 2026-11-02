# Mimi Agent

**Judges:** start with [`JUDGES.md`](JUDGES.md): the hosted demo, what is new
in the hackathon window, the demo's caps and safety, and an offline run for N0.

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
| `muse-spark-1.3` | Mimi's reasoning: reads each shop's reply burst in context and returns a typed verdict (confirmed, disputed, partial, unclear, unrelated) with the language, a confidence and the exact quote it relied on. With vision input, it also reads payment screenshots into typed fields. It reads free-text answers to the one-tap stock question ("e don finish", "about 25 sachets") into a typed reading |
| `muse-spark-1.3-contributor` | The shop simulator: each fictional shop replies in its own voice |
| `muse-voice-transcribe-1.0` | Speech to text for WhatsApp voice notes: first choice for English notes, fallback for the rest |
| `sam-3.1` | Shelf photos for a credit ask (RAI-271): finds every pack on the shelf; code says which delivered item each is, counts and values it, and the request goes to the owner's approvals as a draft (`mimi/credit/`, `docs/CREDIT.md`) |

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

Stock: the shop never counts or keeps a book. Mimi estimates every item
from exact stock in (the rep's deliveries) minus estimated sales (the
reorder rhythm), all in code, and when an item is likely to run out within 3
days asks one question the shop answers with a tap: "Sunrise Instant
Noodles 70g left? Cartons: 0-2 | 3-5 | 6+". Taps and numbers are read in
code; words ("two", "e don finish", "mo ni meji") by Muse Spark into a typed
reading that code turns into a bucket. The count resets the estimate, and
when the shelf will not last to the next van a restock draft goes to the
distributor's approvals ("Thanks. I've asked Coral Lane Trading to prepare
10 cartons for you. They'll confirm before delivery."). At most one question
per item per week; the distributor sees "likely to run low" with a
confidence, never the shop's counts; nothing is ordered until the
distributor approves. See [`docs/STOCK.md`](docs/STOCK.md).

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
python -m mimi.stock.backtest                           # stock estimator backtest (no model)
META_API_KEY= python -m mimi.stock.eval                 # stock answer eval, replaying recorded answers
META_API_KEY= python -m mimi.sim --stock --transcript   # the low-stock scene
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
python -m mimi.stock.eval --live
python -m mimi.sim --stock --stock-words --transcript
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

### Eval sets: source and frozen hashes (RAI-261, RAI-262)

Every label line in both image sets carries `"source": "synthetic"`: each
file is drawn by code from the seed world, with no real account, name or
face. A real file, if one is ever added, is tagged `real-with-consent`,
redacted, and scored separately. Each label line also holds its image's
SHA-256, so one hash of the label file pins every label and every image.
`tests/test_eval_provenance.py` recomputes both and fails if either set
changes without this table changing too.

| Eval set | Label file | Files | Source | Set SHA-256 (of the label file) |
|---|---|---|---|---|
| Payment screenshots | `eval/proof/screens.jsonl` | 40 | synthetic | `566666af77baed1311cc9881e6523eb227aa4ada84ad8d45d071c9f917c1f4e1` |
| Shelf photos | `eval/credit/shelves.jsonl` | 31 | synthetic | `f059df42437f28e6afbe2db8e38a37dae2e3f05f61731ebc64d2866f32213f25` |

Check by hand: `sha256sum eval/proof/screens.jsonl eval/credit/shelves.jsonl`.

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

## The agent API: console and WhatsApp (RAI-266)

The live agent behind HTTP, so the distributor console and a WhatsApp
webhook talk to the same front door, router and harness as everything above.
`POST /v1/turns` takes one inbound message (idempotent on its message id)
and returns the turn id and Mimi's replies; the console reads threads (with
every reasoning step), shops, approvals and credit, and the owner approves
or rejects. A bearer key per distributor scopes every query to that
distributor; CORS allows `https://console.userail.money` and localhost.

State lives in a durable store (`mimi/store`: SQLite by default, Postgres
through `DATABASE_URL`, alembic `0006` and `0007`): the synthetic book, threads,
messages, steps, intents, approvals and a turn queue with leases. A worker
saves after every step and finishes each turn in one lease-guarded
transaction, so a killed worker loses nothing and doubles nothing: a test
SIGKILLs a real worker process mid-run and checks that every turn completes
exactly once. The book is still all synthetic; the model calls are real.

```bash
make api          # migrate, seed the synthetic world once, serve on :8000 (local demo keys)
make api-worker   # an extra worker process (optional)
docker build -t mimi-agent . && docker run -p 3000:3000 -e MIMI_API_KEYS=... -e META_API_KEY=... mimi-agent
```

Endpoints, settings, auth and the durability rules:
[`docs/API.md`](docs/API.md); OpenAPI: [`docs/api/openapi.json`](docs/api/openapi.json).

## Eval: 10 shops end to end through the API (RAI-272)

Ten simulated shops of the synthetic distributor talk to Mimi through the
real HTTP API (`POST /v1/turns`, `POST /v1/events`), the durable store and
two real worker processes. Shops are played by persona prompts; the owner
approves and rejects through `/v1/approvals`; a second distributor's key
probes every read. One worker is SIGKILLed mid-turn and the other takes the
turn over. Order, Proof, Stock and Credit are hired behind the router.

Scenes: 3 delivery receipts (2 YES, 1 short-goods dispute flagged), 6
payment screenshots (3 genuine read as likely, 3 fakes caught with the
expected rule), 2 stock checks with a restock draft, 2 credit bumps with a
shelf photo to approvals, the owner handled first while shop turns queue,
the worker kill, owner approve and reject, console reads and the
cross-tenant probe.

| Measure | Result | Pass mark |
|---|---|---|
| Scene success | 100% (21/21 with the RAI-301 scenes; 18/18 before) | 90% |
| Routing | 100% over 30 turns | 95% |
| Safety violations, money moves, invented figures, duplicate replies, cross-tenant reads | 0 each | 0 |
| Latency per turn, live (clean live run) | p50 6.7 s, p95 11.6 s | |
| Cost of a full live run (clean live run) | N577.07 (harness N482.25, helper models N71.07, SAM N16.00, router N5.23, personas N2.52) | |

The recorded answers in `eval/e2e/` are one clean live run from an empty
cache, plus a targeted live re-run after the pending-ask fix: everything
the fix did not change replayed, and only the new calls went live (3 binder
calls, 1 receipt-reader call, 3 Order steps for shop-008; N44.08). The
latency in `eval/e2e/e2e.report.md` is from that mostly replayed run, so the
live figures above are the clean run's.

The clean run's one miss: shop-008 answered its receipt with "only 4 ctn of
that small sunrise reach / 2 ctn no come at all", and only a bare YES or NO
bound back to the receipt, so the router sent it to `mimi/escalate-owner`
and nothing was flagged on the delivery. Fixed at the cause: an open ask now
binds the shop's next reply to the helper that asked, the model only
classifying the answer (see "Pending-ask binding" in
[`docs/API.md`](docs/API.md)). Now shop-008's reply binds as `short` and
Order flags the delivery; consent is not recorded.

Pending-ask binding has its own held-out set, written and committed before
the binder: 43 replies to receipt asks in English, Pidgin and
Yoruba-flavoured English, short forms ("2 cartons short", "e no complete",
"ko pe", "YES", "ok received"), damaged and wrong goods, and unrelated
questions, scored in code on route and action (`make binding-eval`,
`eval/binding/binding.report.json`): **43/43 (100%)**, 0 false consents
(pass marks 95%, 0). The first run scored 38/43 because every binder answer
came back empty (a 200-token budget, used up by Muse Spark's reasoning), so
the phrase fallback decided alone; the budget was raised to the router's 700
and nothing else changed before the second and last run. One of 40 binder
answers was still empty at 700 tokens and the fallback bound it correctly.
Recording the set cost about N440 over both runs.

Six bugs the run found are fixed, each with a regression test that fails
on the old code (`tests/test_rai272_regressions.py`,
`tests/test_pending_ask_binding.py`): a shop's worded answer to an open ask
went to the general router; the owner waited
behind older shop turns; the seeded collection account did not match the
one on genuine screenshots; a fake screenshot's shop got no reply, or two,
when the flag parked the turn; the owner brief was invented; the recorded
cache could be torn by a kill mid-write.

Limits: one worker kill per run, not ten; the e2e store is SQLite (Postgres
is covered by the store, API and worker-kill tests in CI); Brand is still
`no_helper_yet`; no Langfuse traces are sent from the eval. The data is
synthetic; the model calls are real.

```bash
make e2e          # replay the recorded Muse Spark, SAM 3.1 and persona answers (CI, no key)
make e2e-live     # record what is missing (needs META_API_KEY), write eval/e2e/e2e.report.*
make binding-eval # pending-ask binding, 43 held-out replies, replayed (CI, no key)
```

## Eval: Mimi hears every message, even mid-task (RAI-301)

A shop writes "Yes all complete. How much I owe now?", sends a voice note
"abeg make am 6 tubs, no be 4" while Stock is drafting, or drops a payment
screenshot while its credit request waits for the owner. Each part of each
message is answered once, by the right helper or by code, and none is lost
(design: "Steering" and "Messages that arrive while a turn runs" in
[`docs/API.md`](docs/API.md)). Code splits a message into parts (typed
sentences, voice-transcript clauses, screenshots, photos, documents); one
Muse Spark call labels each part; code decides: answer it here, its own
follow-up turn right after, absorb it into the running turn at the next
step, a new draft version, one short question, or the owner. Decided
approvals are never edited, money and credit changes are always asked
about first, and nothing moves money.

Held-out set, written and committed before the classifier
(`eval/steer/heldout.jsonl`, 53 synthetic cases, 18 with a voice note, a
screenshot, a shelf photo or a document; scored in code through the real
agent, store, queue and inbox on recorded Muse Spark answers;
`make steer-eval`, `eval/steer/steer.report.json`):

| Measure | Result | Pass mark |
|---|---|---|
| Cases right (every part answered, right action) | **50/53 (94.3%)** | 90% |
| ... multi-part / mid-task / corrections / with media | 14/16, 15/15, 19/20 (+1 ambiguous, +1 document), 17/18 | |
| Lost messages, parts with no disposition, duplicate replies, money moves, edits to decided approvals, pending approvals changed when not corrected | 0 each | 0 |
| Cost of the scoring run (64 Muse Spark calls) | N383.42 | |

The set was seen twice. The first run scored 44/53 (83.0%, zeros held,
N388.78; `eval/steer/run1.log.txt`). Before the second and last run, code
rules changed, and one prompt line was added on the dev set
(`eval/steer/dev.jsonl`, never the held-out): voice transcripts split at new
clauses (Spitch gives no full stops), a correction label with nothing open
counts as the current answer, a screenshot anywhere in a burst goes to Proof,
a screenshot mid-task runs as its own Proof turn, a restock correction with no
number asks, and a shelf photo goes to the pending credit request it supports.
The three remaining misses: a Yoruba-flavoured three-part message where the
router's own model answer did not parse (the router asked to clarify), a voice
promise labelled `adds` and kept with the receipt answer, and one steer answer
that came back empty so the fallback kept the message whole.

The e2e run has three new scenes (fixed words, real Muse Spark): shop-033's
receipt reply with a balance question (consent recorded, a follow-up turn
answers the balance), shop-014's voice note correcting the restock quantity
while its stock turn runs (absorbed at the next step, one draft of 6, one
reply), and shop-020's payment screenshot while its credit request is parked
(Proof answers, the credit approval untouched). **21/21 scenes, routing 100%
over 30 turns, 0 safety violations, money moves, invented figures, duplicate
replies or cross-tenant reads.** The synthetic tenant's daily model cap was
raised from N500 to N1,000 for the run: with the new scenes the run's spend
passed N500 and the cap paused helpers (the cap has its own harness checks).

```bash
make steer-eval   # 53 held-out cases, replayed (CI, no key); 90% and the zeros
make steer-eval-live  # record what is missing (needs META_API_KEY)
```

## Eval: shops order by text, voice or list photo (RAI-302)

A shop orders on WhatsApp ("send 5 carton Indomie, 2 Peak milk", a voice note
or a photo of its handwritten list). Muse Spark only says what the shop asked
for, quoting its words; code matches the catalogue, prices it, checks credit
and writes every reply. Every order waits for the owner or rep; over available
credit is flagged, never allowed; edits before approval make a new version;
decided orders are never changed. See `docs/ORDER.md`.

| Held-out (47 synthetic cases, written before the helper) | Pass mark | Result |
|---|---|---|
| Fully right | 90% | **93.6% (44/47)** |
| Invented prices or items | 0 | **0** |
| Credit extended | 0 | **0** |
| Edits to decided orders | 0 | **0** |

The 3 misses are list photos routed away from Order. Two e2e scenes (a list
photo approved; a voice edit making version 2, version 1 refused with 409)
pass. `make order-eval` replays it offline.

## Eval: stock estimate and one-tap counts

**Backtest** (no model calls): every morning for six weeks, the production
stock check, questions, taps and drafts run day by day on a synthetic
reorder history for the 40 Coral Lane shops' regular lines (shops reorder
when low, with demand drift, market days, stock-outs and stock bought
elsewhere that Mimi never sees). The estimator was tuned on one seed and is
reported on a held-out seed it never saw.

| | Mark | Held-out seed | Same estimator, never asks | Naive baseline |
|---|---|---|---|---|
| Run-out within 2 days (true run-out within 7 days) | >= 70% (proposed) | **74.3%** | 72.0% | 59.4% |
| ... high-confidence items | | 81.8% | | |
| ... true run-out within 3 days | | 81.5% | | |
| Max questions per item per week | <= 1 | **1** | | |
| Restock drafts without the shop's count / orders placed | 0 | **0 / 0** | | |

The proposed 70% bar is **met** on the held-out seed (estimator v0.2; v0.1
was 67.5%). The margin is thin: over six more unseen seeds the mean is
72.3% (70.2% to 75.1%; all six at or just over 70%), and slow lines at small
shops (one or two deliveries a month) still miss most, where one unit is
days of sales. That is why every "likely to run low" carries a confidence.
On the world's own random-basket deliveries, which carry no reorder rhythm,
it is 42% (a stress test, no mark). Item-days that start on an empty shelf
are scored too: a stock-out is the miss that matters most.

**Answer reading**: 30 synthetic answers (taps, numbers, number words,
Pidgin, Yoruba, Igbo, Hausa, packets of a carton, ranges, a burst, vague
answers, an injection), through the production flow, recorded live on Muse
Spark 1.3 and replayed in CI: **30 of 30** correct bucket (mark 95%; code
only: 33%), 18 of 18 exact numbers, all 6 unclear answers got one follow-up
and no guess, nothing drafted from the injection. Details and limits:
[`docs/STOCK.md`](docs/STOCK.md).

## Tracing and experiments with Langfuse (optional)

Langfuse is optional and **off unless both `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY` are set**. Without them (and always in CI) the
exporter is a no-op: no client, no thread, no network call. The offline
replay evals stay the CI gate.

With it on, every shop turn becomes one trace (`handle-receipt-reply`,
`handle-voice-note`, `check-payment-screenshot`, `check-shop-stock`,
`handle-stock-answer`, ...) with the model calls as
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
make trace-stock          # low-stock scene traced (env demo); prints a public link to a draft trace
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
| `mimi/stock` | Stock: estimator v0.2 (stock in minus estimated sales, with confidence), trigger and weekly caps, one-tap question and answer reading (code for taps, Muse Spark for words), restock draft and verifier, the backtest and the answer eval |
| `mimi/observe` | Optional Langfuse tracing: turn traces, redaction, the evals as datasets and experiments (off without keys) |
| `mimi/sim` | Shop personas and a one-day simulator (shops can pay by screenshot), and the low-stock scene |
| `mimi/api` | The HTTP API (FastAPI) for the console and the WhatsApp webhook: turns, threads, shops, approvals, credit, health; bearer keys per distributor; the durable agent (front door, router, harness over the store) and its workers |
| `mimi/store` | The durable store: tables and migration, `SqlGo` (the system-of-record ports over the database), the turn queue with leases and checkpoints, the context builder's stores, the synthetic seed |
| `mimi/types.py` | `ShopScope` and the kobo money unit |

Docs: [`docs/HACKATHON.md`](docs/HACKATHON.md) (what was built when),
[`docs/API.md`](docs/API.md),
[`docs/RUNTIME.md`](docs/RUNTIME.md), [`docs/HARNESS.md`](docs/HARNESS.md),
[`docs/WORLD.md`](docs/WORLD.md),
[`docs/RUN.md`](docs/RUN.md), [`docs/VOICE.md`](docs/VOICE.md),
[`docs/PROOF.md`](docs/PROOF.md), [`docs/STOCK.md`](docs/STOCK.md),
[`docs/COMPLIANCE.md`](docs/COMPLIANCE.md),
[`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md).

## Built before the event (not in this repo)

* The marketing website, userail.money.
* A console UI prototype (it calls this repo's HTTP API; it is not in this
  repo).
* An earlier Mimi backend, in a separate repository.

None of these is in this repository, and nothing here imports or calls them.
Every commit here was made during the hackathon window;
`python scripts/hackathon_provenance.py` checks it in CI.

## Licence

Copyright (c) 2026 Rail Technology Concepts Ltd. All rights reserved. The
source is published for hackathon judging; see [`LICENSE`](LICENSE).
