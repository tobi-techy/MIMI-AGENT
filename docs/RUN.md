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

## 4. Clock modes and persistence

```bash
# Fast-forward (default): the sim clock jumps from event to event
python -m mimi.sim --hours 26

# Real time for a demo: 1 = wall clock, 60 = one sim hour per minute
python -m mimi.sim --shops 3 --realtime 60 --transcript

# Persist jobs, shop replies and voice-reply consents (SQLite creates the tables)
python -m mimi.sim --db sqlite:////tmp/mimi-sim.db

# Postgres: install the extra, then create the tables with alembic first
# (revisions 0001 to 0004). MIMI_DB_URL is a SQLAlchemy URL for your own
# local database, set in your shell (never written into a file in this repo).
# Revision 0003 adds the voice-note columns (transcript, never audio);
# 0004 the shops' voice-reply choices, each with its consent message.
pip install -e '.[postgres]'
alembic upgrade head
python -m mimi.sim --db "$MIMI_DB_URL"
```

Jobs land in `runtime_jobs` (the durable state machine, readable by the
console), shop replies in `shop_inbox` and voice-reply choices in
`shop_voice_consent`.

## 5. Tests

```bash
META_API_KEY= pytest          # the whole suite, offline
```
