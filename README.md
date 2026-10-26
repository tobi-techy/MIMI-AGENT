# Mimi Agent

Mimi is a set of WhatsApp agents for FMCG distributors in Nigeria. A
distributor's van drops goods at dozens of small shops a day, and most
arguments about money start with "that is not what you delivered". Mimi sends
each shop a receipt on WhatsApp right after the drop, reads the reply in
whatever language the shop writes or speaks (English, Pidgin, Yoruba, Igbo,
Hausa or a mix, typed or as a voice note), records the shop's confirmation,
and flags any dispute to the owner with the shop's own words. When a shop
promises to pay ("I go pay 20k tomorrow"), Mimi reads the amount and the day
back and notes the promise only after the shop says YES.

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
| `muse-spark-1.3` | Mimi's reasoning: reads each shop's reply burst in context and returns a typed verdict (confirmed, disputed, partial, unclear, unrelated) with the language, a confidence and the exact quote it relied on |
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
```

**Live** (real Muse Spark calls):

```bash
export META_API_KEY=...        # your Meta Model API key; never commit it
export SPITCH_API_KEY=...      # Spitch key, for voice notes; never commit it
python -m mimi.sim --shops 3 --transcript
python -m mimi.receipts.eval --live
python -m mimi.voice.eval --live
python -m mimi.sim --shops 3 --transcript --voice 1 --promises 1 --voice-replies
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

## What is in this repo

| Package | What it does |
|---|---|
| `mimi/runtime` | Always-on durable jobs: state machine, leases with heartbeat and takeover after a crash, outbox with idempotency keys so nothing is sent twice, step, time and naira budgets, owner messages taken at the next step |
| `mimi/world` | The synthetic distributor: 40 shops, 30 invented SKUs, 8 weeks of deliveries, payments, bank feed and fake transfer alerts, from a fixed seed |
| `mimi/models` | Meta Model API client, FakeModel, record and replay cache |
| `mimi/receipts` | The delivery receipt flow, the reply reader and its guards, payment-promise read-back, voice-reply scripts, the eval |
| `mimi/voice` | Voice notes: a transcription port with Spitch and Muse Voice Transcribe adapters, a router, audio checks and ffmpeg conversion, money and date reading, voice-reply consent and text to speech, fakes, record and replay, the voice eval |
| `mimi/sim` | Shop personas and a one-day simulator |
| `mimi/types.py` | `ShopScope` and the kobo money unit |

Docs: [`docs/HACKATHON.md`](docs/HACKATHON.md) (what was built when),
[`docs/RUNTIME.md`](docs/RUNTIME.md), [`docs/WORLD.md`](docs/WORLD.md),
[`docs/RUN.md`](docs/RUN.md), [`docs/VOICE.md`](docs/VOICE.md),
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
