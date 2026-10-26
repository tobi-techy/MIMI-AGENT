# Voice notes

A shop owner can answer a delivery receipt with a WhatsApp voice note in
English, Pidgin, Yoruba, Igbo, Hausa or a mix, often with market noise
behind it. Mimi turns the note into text and reads it exactly like a typed
reply. Speech to text is the only thing the speech services do: **Muse Spark
still does all the reasoning** (the verdict), and the code guards still
decide what happens.

Two things build on that (RAI-290, second part):

* **Payment promises.** "I go pay 20k tomorrow": code reads the amount in
  integer kobo and the day in Africa/Lagos, Mimi reads both back in plain
  English, and only the shop's own YES records the promise. Mimi never moves
  money.
* **Voice replies for shops that opt in.** A shop that sends "VOICE ON"
  also gets Mimi's short replies as a voice note in its language. The text
  always goes too, with every amount and day in it.

## The path of a voice note

1. The webhook carries the audio (`ShopWebhook.audio`). The desk checks the
   message id first, so a webhook retry is never transcribed twice.
2. `mimi/voice/audio.py` checks the clip: Ogg, MP3, WAV or M4A by its first
   bytes, at most 4 MB and 180 seconds, at least 0.3 seconds.
3. The router (`mimi/voice/router.py`) sends it to Spitch or Muse Voice
   Transcribe, with the shop's usual language as a hint and the receipt's
   brand and product words as keywords. Each call has a timeout and one
   retry.
4. `mimi/voice/quality.py` checks the text, because neither API returns a
   confidence score. Too few words for the length, impossible speed, mostly
   symbols, or one word looping marks it `low`. Empty text is `failed`.
5. The inbox stores the transcript (`kind="voice"`, status, provider, any
   API confidence, clip length and SHA-256). The audio is never stored.
6. The flow judges it with the same `ReceiptJudge`. The model sees it marked
   as `"source": "voice_note_transcript"` with its quality. The evidence
   quote must be found in the transcript.
7. Guards for voice (`mimi/receipts/decide.py`, `flow.py`):
   * a `low` or `failed` transcript can never produce consent, as witness or
     anywhere in the burst;
   * when every new message is an untranscribable voice note, the model is
     not asked: Mimi sends "Please type your reply" (plain English), and a
     second unreadable burst flags the owner (`voice_unreadable`);
   * a confirm held back because of a doubtful transcript also gets the
     request to type;
   * owner flags show voice notes as `[voice note transcript] ...`.

## API shapes found (26 Oct 2026)

Checked against each provider's own reference and with live calls on
synthetic audio. Nothing was assumed.

### Muse Voice Transcribe (`muse-voice-transcribe-1.0`, Meta Model API)

Listed by `GET https://api.meta.ai/v1/models`. Reference:
`https://dev.meta.ai/docs/api-reference/voice/transcribe` and
`https://dev.meta.ai/docs/speech-to-text`.

* `POST https://api.meta.ai/v1/asr/transcribe`, `Authorization: Bearer`,
  `Accept: application/json`, `multipart/form-data` with two parts:
  * `request` (JSON): `model`, `audioEncoding: "WAV"` (the only value),
    `mode: "PUSH_TO_TALK"` (also `ENDPOINTING`, `DIARIZATION`), optional
    `keywords` (list) and `languageBias` (list of language names);
  * `audio`: mono 16-bit PCM WAV at 16 or 24 kHz.
* Response: `{"sessionId", "transcript", "audioDurationMs", "turns"}`
  (`turns` empty in `PUSH_TO_TALK`). No confidence, no detected language.
* An Ogg voice note is refused: HTTP 400 "The 'audio' part must be a mono
  16-bit PCM WAV at 16 or 24 kHz". Mimi converts with ffmpeg first.
* Limits: 10 minutes, 32 MB. 25 listed languages with code-switching;
  English is one, Yoruba, Igbo, Hausa and Pidgin are not.
* Price: USD 0.18 per hour of audio.

### Spitch speech to text

Reference: `https://docs.spitch.app/features/transcription` and
`https://api.spitch.app/openapi.json` (api 0.1.46).

* `POST https://api.spitch.app/v1/transcriptions`, `Authorization: Bearer`,
  `multipart/form-data`: `content` (the audio file; Ogg, MP3, WAV, M4A as
  they are, up to 25 MB), `language` (`en`, `yo`, `ig`, `ha`, `am`; omit for
  automatic handling), `special_words` (comma-separated), `timestamp`
  (`none`, `sentence`, `word`).
* Response: `{"request_id", "text", "segments", "timestamps",
  "detected_language"}`. No confidence.
* There is no Pidgin code; Mimi sends `en` for a Pidgin hint.
* `X-Data-Retention: false` is sent on every call, so Spitch keeps no copy.
* Price: USD 0.00042 per second of audio.
* In our tests `special_words` did not change the noisy Hausa clip's
  "windows" back to "noodles".

### Spitch text to speech (fixtures and voice replies)

`POST https://api.spitch.app/v1/speech`, JSON `text`, `voice`, `language`,
`format`, `speed`; streamed audio back. `language` must be an ISO 639 code
(`pcm` is refused with HTTP 400), so Pidgin uses a Pidgin voice with `en`.
USD 0.0014 per second generated. The WAV comes back with a streaming header
(length unknown), so Mimi sets the real sizes before reading its length.
On one Yoruba fixture sentence (`vn-31`, about 4 seconds of text) the
`sade` voice ran on for 15 seconds, and 22 seconds on a retry; the first
take is kept as a stress case.

## Router policy, checked again by the 50-clip eval

Tobi's starting rule: Spitch for Yoruba, Igbo and Hausa and as the default
for Nigerian voice notes, Muse as the fallback, the pick decided by the
numbers. The first 16-clip eval moved English to Muse first. The 50-clip
eval keeps that policy: with a hint, the router is the best or tied best in
every language.

| Language hint | First | Fallback | Why (50-clip eval, with hints) |
|---|---|---|---|
| Yoruba, Igbo, Hausa | Spitch | Muse | Spitch 48%, 111% and 14% WER against Muse 81%, 80% and 80%; Muse does not list these languages |
| Pidgin | Spitch | Muse | 24% against 26% WER, 11 against 10 verdicts right; Muse once heard "don land" as "don't land" |
| English | Muse | Spitch | 3% against 11% WER, the same verdicts |
| none | Spitch | Muse | covers every Nigerian language |

A `low` transcript also falls back: on the noisy Igbo dispute Spitch looped
("northern rock" over and over), the check marked it `low`, and Muse's
transcript was read correctly. That is why the router's Igbo WER (34%) is
far under either service alone.

**With no language hint** the router falls to 28% WER and 43 of 50
verdicts (22% and 46 of 50 with hints). Most of the loss is English and
Pidgin: with no hint the router sends English notes to Spitch first (11%
WER instead of Muse's 3%), and Spitch with no hint is worse on Pidgin (34%
against 24%) and Igbo (120% against 111%). So the shop's usual language
should always be passed as the hint; automatic detection is the fallback,
not the plan.

## Payment promises

Shops answer a receipt with promises all the time: "Everything don land. I
go pay 20k tomorrow." `mimi/voice/promise.py` reads them in code, from a
voice note's transcript or a typed message:

* amounts: `20k`, `20 k`, `2.5k`, `20,000`, `N20,000`, `₦20,000`,
  `20,000 naira`, "twenty thousand", "fifteen thousand five hundred",
  Yoruba `ẹgbẹ̀rún mẹ́wàá`, Igbo `puku naira iri abụọ`, Hausa
  `dubu ashirin`, in integer kobo;
* days: today, tomorrow, next tomorrow, a weekday, "next week Monday",
  "end of the month", "the 30th", "30th October", "30/10", Yoruba `ọ̀la`,
  `ọ̀túnla`, `Ọjọ́ Ẹtì`, Igbo `echi`, `Fraịdee`, Hausa `gobe`, `jibi`,
  `Juma'a`, resolved in **Africa/Lagos from the message's own timestamp**;
* paying: pay, balance, settle, transfer, Yoruba `san`, Igbo `akwụ`, Hausa
  `biya`. "I don pay", "mo ti san" and "na biya" are claims of payment, not
  promises, and are never recorded as one.

The result is typed (`PromiseReading`: `promise`, `unclear`, `paid_claim`
or `none`, with a `PaymentPromise` of `amount_kobo` and `due_on`). It is
`unclear`, with the reason, when the amount or the day is missing, there are
two of either, a bare number under 1,000 could be naira or thousands ("I go
pay 20 tomorrow"), the weekday is today's (this week or next?), the day has
passed, or the transcript is `low`.

What Mimi does (`mimi/receipts/promises.py`, `flow.py`):

1. The delivery is settled first, exactly as before (consent, or an owner
   flag). Then, if a message promised to pay:
2. a clear promise is **read back in plain English**: "You said you will pay
   N20,000 on Tuesday 27 October 2026 for delivery dlv-56-9039. Reply YES
   if that is right, or tell me the right amount and day. I only note your
   promise; I do not take or move money." An unclear one gets **one plain
   question** ("On what day will you pay N20,000 for delivery ...?").
   Nothing is recorded;
3. `RecordPaymentPromise` (amount in kobo, ISO day, timezone, the message
   it was heard in, the read-back and its digest, the shop's YES) is
   proposed **only after the shop's YES to that exact read-back**. The YES
   is checked in code, on a typed message or a voice note whose transcript
   is `ok`. A YES that brings a different amount or day is read back again;
   a "no" or anything else gets "Please tell me the right amount and day";
4. at most three messages about one promise; after that, or with no answer
   in 24 hours, nothing is recorded. The delivery's own result stands.

The intent's kind (`record_payment_promise`) carries no money verb and is
registered like the others; it is a note for the books and the owner.

## Voice replies (opt in)

* A shop sends `VOICE ON` (or `VOICE ON YORUBA`, ... `HAUSA`) or `VOICE
  OFF`. The desk records each choice in `shop_voice_consent` (alembic
  `0004`) with the shop's message id, words, sender and time, and does not
  route it to a receipt. Default: off. Nobody but the shop can turn it on.
* Spoken: the clarifying question, the request to type, a promise
  read-back and a promise question. The receipt itself (a list of lines and
  figures) is text only.
* Scripts are fixed templates (`mimi/receipts/voice_replies.py`), never
  model-written. English and Pidgin say the amount and the day in words
  ("fifty thousand naira on Saturday the thirty-first of October"), built
  from the same kobo and date as the text. Yoruba, Igbo and Hausa ask in
  that language and point to the text for the figures. A check in code
  drops the voice (text only) if the script says any amount or day the
  text does not.
* Voices: English `lucy`, Pidgin `tega` (with `en`), Yoruba `funmi`, Igbo
  `ngozi`, Hausa `amina`; Ogg/Opus at 16 kbps like a WhatsApp voice note.
  A failed speech call sends the text alone.
* No audio is kept. The audio goes to the channel and is dropped;
  `CachedSpeaker` writes audio to disk only when a caller gives it a
  directory (the recorded smoke run, outside the repo).
* Round trip (smoke, 26 Oct): the English read-back said "31 October" and
  Spitch heard it back as "thirty fourth october", so dates are now spoken
  as ordinal words; the new script was heard back as "fifty thousand naira
  on saturday the thirty first of october".
* The Yoruba, Igbo and Hausa scripts were written for this repo and have not
  yet been reviewed by a native speaker.

## Eval (50 clips)

`python -m mimi.voice.eval` (replays recorded answers; CI) and `--live`
(Spitch, Muse Voice Transcribe and Muse Spark). 50 synthetic clips
(`eval/voice/clips.jsonl`): 13 English, 13 Pidgin, 7 Yoruba, 6 Igbo, 7
Hausa, 4 code-switched (Yoruba, Igbo and Hausa with English); 9 confirm, 7
dispute, 6 partial, 6 unclear, 2 unrelated, 14 with a clear payment
promise, 5 with an unclear one, 1 "I don pay" claim; 8 clean, 27 light
noise, 15 heavy noise. Every clip runs through each transcriber twice, with
the shop's language as a hint and with none, and then end to end through
the production `ReceiptFlow` (Muse Spark verdict, guards, promise read-back).
When Mimi reads a promise back, a truthful shop answers: YES if the read-back
is right, "No" if not. Recorded 26 Oct 2026, judge `muse-spark-1.3`. Full
report: `eval/voice/voice.report.json`.

| Transcriber | WER (folded) | Verdicts right | False confirms | Disputes flagged first read | Disputes ever confirmed | Amount and day right | Promises recorded without YES |
|---|---|---|---|---|---|---|---|
| Spitch | 31% (28%) | 45 of 50 | 0 | 12 of 15 | 0 | 13 of 14 | 0 |
| Muse Voice Transcribe | 41% (36%) | 38 of 50 | 0 | 10 of 15 | 0 | 10 of 14 | 0 |
| Router (production) | 22% (18%) | 46 of 50 | 0 | 13 of 15 | 0 | 13 of 14 | 0 |
| Spitch, no hint | 35% (32%) | 43 of 50 | 0 | 11 of 15 | 0 | 12 of 14 | 0 |
| Muse, no hint | 42% (38%) | 33 of 50 | 0 | 7 of 15 | 0 | 9 of 14 | 0 |
| Router, no hint | 28% (24%) | 43 of 50 | 0 | 11 of 15 | 0 | 12 of 14 | 0 |

Per language (WER / verdicts right):

| Language | Spitch WER / verdicts | Muse Voice Transcribe WER / verdicts | Router (production) WER / verdicts | Spitch, no hint WER / verdicts | Muse, no hint WER / verdicts | Router, no hint WER / verdicts |
|---|---|---|---|---|---|---|
| english (13) | 11% / 13 of 13 | 3% / 13 of 13 | 3% / 13 of 13 | 11% / 13 of 13 | 2% / 13 of 13 | 11% / 13 of 13 |
| hausa (7) | 14% / 6 of 7 | 80% / 3 of 7 | 14% / 6 of 7 | 14% / 6 of 7 | 73% / 2 of 7 | 14% / 6 of 7 |
| igbo (6) | 111% / 5 of 6 | 80% / 4 of 6 | 34% / 6 of 6 | 120% / 4 of 6 | 80% / 2 of 6 | 43% / 4 of 6 |
| mixed (4) | 33% / 4 of 4 | 57% / 3 of 4 | 33% / 4 of 4 | 33% / 4 of 4 | 59% / 3 of 4 | 33% / 4 of 4 |
| pidgin (13) | 24% / 11 of 13 | 26% / 10 of 13 | 24% / 11 of 13 | 34% / 10 of 13 | 29% / 9 of 13 | 34% / 10 of 13 |
| yoruba (7) | 48% / 6 of 7 | 81% / 5 of 7 | 48% / 6 of 7 | 48% / 6 of 7 | 83% / 4 of 7 | 48% / 6 of 7 |

WER is corpus WER after lower-casing, dropping punctuation and reading
number words as digits; in brackets with Yoruba and Igbo tone marks folded.
WER has no pass mark. "Amount and day right" counts the 14 clips with a
clear promise where the transcript gave exactly the right kobo and day;
the router missed only `vn-31`, the clip where the speech engine ran on
("egberun mewarun mewarun"): the amount is unreadable, so Mimi asks and
records nothing. Every read-back the router sent (10) was right, and every
one was recorded correctly after the YES. No transcriber ever heard a
promise in a clip that had none.

Safety marks, which fail CI: **0 false confirms, 0 disputes that ever end
in consent, 0 promises recorded without the shop's YES (or recorded wrong)**,
for all six transcriber and hint runs. Met.

The first-read dispute mark stays a warning unless `--strict` is set
(Tobi's call, 26 Oct). The router flags 13 of 15 disputes on the first read.
The other two are read as `unclear`, so Mimi asks once and then flags the
owner; neither can be confirmed: `vn-16` (noisy Hausa, "noodles" heard as
"windows") and `vn-29` (noisy Pidgin "Una short me two carton of noodles",
heard as "but not shot mean to carton of nudus").

Payment-only clips ("I will pay fifteen thousand naira on Friday", "Make I
pay 15,000 for Friday") are not a delivery confirmation: Muse Spark read
them as `unclear` or `unrelated`, never `confirmed`.

Cost to record (from the caches): Spitch speech to text 412 s of audio,
USD 0.17; Muse Voice Transcribe 412 s, USD 0.02; Spitch text to speech for
the 50 fixtures about 161 s, USD 0.23. Muse Spark: about 65,600 prompt and
31,000 completion tokens per run of 50 clips.

## Fixtures

All clips are synthetic-generated: Spitch text-to-speech system voices
speaking text we wrote, then ffmpeg noise (pink and brown noise, a 100 Hz
hum, and for heavy noise two short 440 Hz horn tones; nothing sampled).
Light noise sits about 19 dB under the speech, heavy about 8 dB. No real
person's voice is in this repo. Made by `scripts/make_voice_fixtures.py`,
50 clips, about 430 KB in total, mostly Ogg/Opus at 16 kbps (WhatsApp's
format), one MP3 and one M4A.

## Not done yet

* Real voice notes: only later, only with the speaker's consent, and kept
  out of this public repo (`COMPLIANCE.md`).
* Native-speaker review of the Yoruba, Igbo and Hausa voice-reply scripts
  and of the promise words.
* One promise per message: "10k today and 10k Friday" (instalments) is
  asked about, not split into two promises.
* Igbo market days (Eke, Orie, Afọ, Nkwọ) are not read as days; Mimi asks.
* WhatsApp media upload and download (the Cloud API's media ids, with the
  business token) are outside this repo: the desk takes the bytes in, and
  the sender hands the voice reply out.
