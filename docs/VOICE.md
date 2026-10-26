# Voice notes

A shop owner can answer a delivery receipt with a WhatsApp voice note in
English, Pidgin, Yoruba, Igbo, Hausa or a mix, often with market noise
behind it. Mimi turns the note into text and reads it exactly like a typed
reply. Speech to text is the only thing the speech services do: **Muse Spark
still does all the reasoning** (the verdict), and the code guards still
decide what happens.

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

### Spitch text to speech (fixtures only)

`POST https://api.spitch.app/v1/speech`, JSON `text`, `voice`, `language`,
`format`, `speed`; streamed audio back. `language` must be an ISO 639 code
(`pcm` is refused with HTTP 400), so Pidgin clips use a Pidgin voice with
`en`. USD 0.0014 per second generated.

## Router policy, decided by the eval

Tobi's starting rule: Spitch for Yoruba, Igbo and Hausa and as the default
for Nigerian voice notes, Muse as the fallback, the pick decided by the
numbers. The numbers changed one line: English voice notes go to Muse first.

| Language hint | First | Fallback | Why |
|---|---|---|---|
| Yoruba, Igbo, Hausa | Spitch | Muse | Spitch's WER is far lower; Muse does not list these languages |
| Pidgin | Spitch | Muse | 27% against 32% WER, and Muse heard "don land" as "don't land" |
| English | Muse | Spitch | 5% against 16% WER, same verdicts |
| none | Spitch | Muse | covers every Nigerian language |

A `low` transcript also falls back: on the noisy Igbo dispute Spitch looped
("northern rock" over and over), the check marked it `low`, and Muse's
transcript was read correctly as a dispute.

## Eval

`python -m mimi.voice.eval` (replays recorded answers; CI) and
`--live` (Spitch, Muse Voice Transcribe and Muse Spark). 16 synthetic clips
(`eval/voice/clips.jsonl`): 4 English, 4 Pidgin, 3 Yoruba, 1 Yoruba-English,
2 Igbo, 2 Hausa; 7 confirm, 5 dispute, 1 partial, 2 unclear, 1 unrelated;
2 clean, 8 light noise, 6 heavy noise. Recorded 26 Oct 2026, judge
`muse-spark-1.3`. Full report: `eval/voice/voice.report.json`.

| Language | Spitch WER | Spitch verdicts | Muse WER | Muse verdicts | Router WER | Router verdicts |
|---|---|---|---|---|---|---|
| English (4) | 16% | 4/4 | 5% | 4/4 | 5% | 4/4 |
| Pidgin (4) | 27% | 4/4 | 32% | 4/4 | 27% | 4/4 |
| Yoruba (3) | 42% | 3/3 | 67% | 3/3 | 42% | 3/3 |
| Yoruba-English (1) | 12% | 1/1 | 88% | 1/1 | 12% | 1/1 |
| Igbo (2) | 323% | 1/2 | 85% | 2/2 | 62% | 2/2 |
| Hausa (2) | 35% | 1/2 | 88% | 0/2 | 35% | 1/2 |
| **All (16)** | 52% | 14/16 | 48% | 14/16 | **26%** | **15/16** |

| | Spitch | Muse | Router |
|---|---|---|---|
| False confirms | **0** | **0** | **0** |
| Disputes flagged on first read (6) | 4 | 5 | 5 |
| Disputes that ever end in consent | 0 | 0 | 0 |
| Confirms recorded (7) | 7 | 6 | 7 |
| Transcription latency, mean | 1.98 s | 2.44 s | 2.08 s |

WER is corpus WER (all errors over all reference words) after lower-casing,
dropping punctuation and reading number words as digits; with Yoruba and
Igbo tone marks folded it is 50%, 41% and 23%. Spitch's 323% on Igbo is one
looping transcript. WER is reported as measured; it has no pass mark.

Pass marks: 0 false confirms (met, every transcriber) and every dispute
caught end to end. **The second is not fully met.** The router flags 5 of 6
disputes on the first read. The sixth, noisy Hausa ("Noodles ba su cika
ba..."), was heard by both services as "windows ba su cika ba ..."; Muse
Spark said something was incomplete but named no receipt line, so it
answered `unclear`. Mimi asks once more and then flags the owner; it can
never confirm. The eval exits non-zero only when a safety mark fails (a
false confirm, or a dispute that ends in consent); `--strict` also fails on
the first-read mark.

## Fixtures

All clips are synthetic-generated: Spitch text-to-speech system voices
speaking text we wrote, then ffmpeg noise (pink and brown noise, a 100 Hz
hum, and for heavy noise two short 440 Hz horn tones; nothing sampled).
Light noise sits about 19 dB under the speech, heavy about 8 dB. No real
person's voice is in this repo. Made by `scripts/make_voice_fixtures.py`,
16 clips, 138 KB in total, mostly Ogg/Opus at 16 kbps (WhatsApp's format),
one MP3 and one M4A.

## Not done yet

* Real voice notes: only later, only with the speaker's consent, and kept
  out of this public repo (`COMPLIANCE.md`).
* No language hint pass in the eval (every clip had its shop's language);
  automatic detection is untested.
* WhatsApp media download (the Cloud API gives a media id, fetched with the
  business token) is outside this repo; the desk takes the bytes.
