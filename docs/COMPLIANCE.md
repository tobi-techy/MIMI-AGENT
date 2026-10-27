# Meta Global AI Developer Hackathon: rules compliance

Sources: the Official Rules (dated 25 Oct 2026, read 26 Oct 2026), and the
organisers' ruling of 26 Oct 2026 (Cerebral Valley): "Judges must clearly
identify what you built during the event, so keep the pre-existing work
separate. Put only the work done during the hackathon in a fresh public repo,
and open your 3 minute demo video with one line stating what was pre-existing
and what you built this week. Any hosted demo needs to give judges access and
instructions."

Build window: 26 Oct 2026 12:00 PM ET (16:00 UTC, 17:00 WAT) to 4 Nov 2026
9:00 PM ET (5 Nov 02:00 UTC, 03:00 WAT). Track: Startup.

## Rule by rule

| Rule | How we meet it |
|---|---|
| Original work, solely owned | Mimi is Rail Technology Concepts Ltd's own code, written by Tobi Omotade, the participant. Copyright notice in `LICENSE` and `pyproject.toml`. |
| Third-party technical help allowed if the work stays the participant's own | AI coding assistants were used for parts of the implementation, under the participant's direction and review. All output is owned by Rail Technology Concepts Ltd. |
| Open source allowed if licences are followed | Every package is listed with its licence in `DEPENDENCIES.md`: MIT, BSD-3-Clause, Apache-2.0, PSF-2.0, MPL-2.0 (certifi, unmodified) and LGPL with exceptions (psycopg2-binary, optional, a dynamically loaded driver, unmodified). ffmpeg (LGPL-2.1 or later) is run as a separate program, never linked. None requires releasing our code. |
| Third-party APIs | Spitch (speech to text; text to speech for the fixtures and for voice replies to shops that opted in) is a paid, usage-based third-party API used under its terms, with data retention switched off (`X-Data-Retention: false` on every call). Listed in `DEPENDENCIES.md`. |
| No infringement of IP, privacy or other rights | Data is fully synthetic (below). Brands are invented. Bank and fintech names appear only as text in fake alert layouts, never with logos. No real person, phone number or account number. |
| Build window | Every commit in this repo is authored and committed after 26 Oct 2026 16:00 UTC. `scripts/hackathon_provenance.py` prints each one in WAT and fails CI if one is earlier. |
| Pre-existing work kept separate | This is a fresh repo with only window work. Pre-existing work is named in `HACKATHON.md` and the README and is not here. The demo video opens with one line naming what was pre-existing and what was built this week. |
| Hosted demo gives judges access and instructions | If a hosted demo is submitted: judge access and a short how-to in the README. |
| Judging: use of Meta AI tooling | See "Meta tooling" below. |

## Provenance

* The repo was created on 26 Oct 2026 with a README and the licence, inside
  the window.
* `python scripts/hackathon_provenance.py` lists every commit with its author
  and commit dates in WAT, fails if any is before the window opened, and marks
  any made after it closed. CI job `hackathon provenance` runs it on every
  push and pull request.

## Secrets hygiene

* `.secrets.baseline` (detect-secrets 1.5.0) records no findings. The
  recorded model answers in `eval/receipts/receipt_replies.cache.json` are
  excluded from the scan because their keys are SHA-256 hashes of the
  requests; they hold no key or personal data.
* gitleaks scans the whole history in CI.
* `META_API_KEY` is read only in `mimi/models/env.py` and
  `mimi/voice/env.py` and passed to the clients; `SPITCH_API_KEY` only in
  `mimi/voice/env.py` (and the fixture script). Neither is ever logged,
  printed or committed.
* The recorded voice answers (`eval/voice/*.cache.json`) are excluded from
  the detect-secrets scan for the same reason as the receipt cache: their
  keys are SHA-256 hashes. They hold transcript text of synthetic clips
  only, never audio. Database URLs come from
  the shell (`MIMI_DB_URL`), never from a file in the repo.
* The recorded reply-judge answers (`eval/judge/replies.cache.json`) are
  excluded from the detect-secrets scan for the same reason: their keys are
  hashes of the requests, and the values are the judge's JSON on synthetic
  replies. Test keys in `tests/test_observe.py` are fake (`sk-lf-test`, a
  made-up hash) and marked `pragma: allowlist secret`.
* `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` come from the shell only,
  are read only in `mimi/observe/langfuse_export.py` and the
  `scripts/langfuse_*.py` / `scripts/trace_smoke.py` scripts, and are never
  logged, printed or committed. CI checks that neither is set and that the
  exporter is off there; the two optional Langfuse workflows read them from
  repository secrets and never run on pull requests.

## Ownership

Copyright (c) 2026 Rail Technology Concepts Ltd. All rights reserved
(`LICENSE`). The source is published for judging; no licence to use, copy,
modify or distribute it is granted without written permission.

## Data

* Fully synthetic world: Coral Lane Trading (fictional distributor), 40
  fictional shops in real Ado-Ekiti areas, 30 invented-brand SKUs, 8 weeks of
  deliveries, payments and bank feed, fixed seed (`WORLD.md`).
* Phones use `+2340000...` (never assigned), including the eval's and the
  tests'. Account numbers are fictional and masked.
* The receipt-reply eval (`eval/receipts/receipt_replies.jsonl`) is synthetic
  and hand-written; every case carries `"synthetic": true`.
* The recorded model answers are Muse Spark's outputs on those synthetic
  cases.
* Payment screenshots (`eval/proof/screens/`, 40 JPEGs) are synthetic:
  drawn with Pillow by `scripts/make_proof_fixtures.py` for four fictional
  banks (Kestrel Bank, Harmattan MFB, Lagoonpay, Ironleaf Bank) with logos
  drawn from plain shapes, the world's fictional shops and owners, account
  numbers starting 999 and `+2340000...` phones. No real bank's name, logo,
  layout or customer data, and no real screenshot. The sim draws the same
  kind on the fly. Real screenshots, if ever used, only with the shop's
  consent, reported separately and kept out of this public repo.
* In the product, Mimi keeps what it read off a screenshot plus the image's
  SHA-256 and size (`payment_proofs`, alembic `0005`), never the image.

## Observability (Langfuse, optional)

Tracing is off without both Langfuse keys and always off in CI. When it is
on, every value goes through `mimi/observe/redact.py` before it reaches the
SDK, and the SDK's `mask` hook runs the same redaction again:

* **Phone numbers:** only the last 4 digits stay (`+234******4567`,
  `0*******4567`).
* **Any run of 10 or more digits** (account numbers, session ids): only the
  last 4 digits stay. Fields named like `account`, `phone`, `msisdn`,
  `nuban` are masked to the last 4 whatever their shape.
* **Keys and tokens** (`Bearer ...`, `sk-...`, `pk-lf-...`/`sk-lf-...`,
  `api_key=`, `secret:`, `password=`, `token=`, `authorization`):
  replaced by `[redacted]`.
* **Raw media** (image or audio bytes, `data:...;base64,...` URLs) never
  leaves; it becomes its SHA-256 and size.
* **Only synthetic media is attached** (the drawn screenshot, the generated
  voice clip), and only when the turn is marked synthetic and
  `MIMI_TRACE_MEDIA` is not `0`. A real shop's image or audio is never sent.
* `user_id` is the shop id, which is pseudonymous; no names or phones are in
  trace attributes. The datasets leave phone numbers out.

The redaction is tested in `tests/test_observe.py` (it never raises, whatever
it is given). Langfuse Cloud (EU) is a third-party service used under its
terms; every trace and dataset in the project is synthetic.

## Voices

* Synthetic voices only. Every clip in `eval/voice/clips/` is
  synthetic-generated: a Spitch text-to-speech system voice reading text we
  wrote, plus ffmpeg-generated noise. Each is labelled in
  `eval/voice/clips.jsonl` (`"synthetic": true`, `"origin":
  "synthetic-generated: ..."`) and carries a `comment` tag saying so. No
  real person's voice, and no recording of anyone, is in this repo.
* Real shop voice notes, if ever used for testing, only with the speaker's
  consent, and kept out of this public repo.
* In the product, Mimi stores a voice note's transcript, its length and its
  SHA-256, never the audio. Spitch is called with data retention off.
* Voice replies are off by default. Only the shop can turn them on, by
  sending "VOICE ON"; every choice is kept with the shop's own message as
  its consent (`shop_voice_consent`, alembic `0004`). The text always goes
  with the voice note and carries every amount and day. Voice-reply audio is
  handed to the channel and dropped; it is written to disk only by the
  recorded smoke run's cache, outside this repo.
* The voice replies are Spitch system voices, not a real person's voice.

## Money

* Mimi never moves money. A payment promise ("I go pay 20k tomorrow") is
  read in code (integer kobo, a day in Africa/Lagos), read back to the shop
  in plain English, and recorded (`RecordPaymentPromise`, a note for the
  books) only after the shop's YES to that read-back. An unclear amount or
  day gets a question and records nothing. The intent carries no money verb
  and the runtime refuses any that does.
* A payment screenshot never makes a payment verified or received. Passing
  the code checks makes it `likely` (`RecordPaymentProof`, level fixed to
  `likely` in its type); only a credit in the distributor's own bank feed
  (a read-only port) makes it verified (`RecordBankWitness`, with the rep's,
  the shop's and the bank's witness). A screenshot that looks wrong goes to
  the owner's approvals with the failed rules named (`ProofForApproval`);
  the shop is told, neutrally, that it was sent to the distributor to
  confirm with the bank, and nothing on its book changes. Mimi never accuses
  a shop, never moves money and never changes credit.

## Assets

No third-party images, logos or fonts are in this repo. The synthetic
payment screenshots are drawn by our own code; their text is rendered with
the system's DejaVu fonts (free licence), which are not copied into the
repo. The bank logos are our own plain shapes for invented banks. Shelf photos for the
demo, if added, will be taken by the participant with the shop owner's
permission and no faces in frame, and each will be listed here.

## Meta tooling used

* **Muse Spark 1.3** (`muse-spark-1.3`, Meta Model API) is Mimi's reasoning
  step: it reads each settled WhatsApp reply burst in context (receipt lines,
  earlier messages) in English, Pidgin, Yoruba, Igbo, Hausa or a mix and
  returns a typed verdict through a JSON schema response format, with code
  guards around it (`mimi/receipts/verdict.py`, `decide.py`). Measured on an
  84-case eval, recorded live.
* **Muse Spark 1.3 contributor tier** (`muse-spark-1.3-contributor`) drives
  the shop simulator personas, each writing in its own voice.
* **Muse Voice Transcribe** (`muse-voice-transcribe-1.0`) turns WhatsApp
  voice notes into text: first choice for English notes and the fallback for
  the rest, behind a router with Spitch (`mimi/voice/`, `docs/VOICE.md`).
  The transcript goes to Muse Spark like any typed reply. Measured on a
  50-clip synthetic voice eval (with and without language hints), recorded
  live.
* Muse Spark 1.3 also judges the tone of Mimi's shop-facing replies in the
  optional Langfuse experiments (`reply-tone`, calibrated on 8 labelled
  replies, answers cached).
* **Muse Spark 1.3 with vision input** reads payment screenshots
  (`read_payment_image`, `mimi/proof/reading.py`): the image goes as an
  `image_url` content part and the fields come back through a JSON schema
  response format; code does every check (`mimi/proof/checks.py`). Measured
  on a 40-screenshot synthetic eval, recorded live (`docs/PROOF.md`).
* Planned during the window: **SAM 3.1** for shelf-photo segmentation.

## Pre-submit checklist

- [x] Fresh public repo with only the window's work.
- [x] Secrets scans in CI (detect-secrets, gitleaks); no key or DSN in the repo.
- [x] README: synthetic data note, offline and live quickstart, eval command.
- [ ] Demo video opens with one line on what was pre-existing and what was
      built this week.
- [ ] Hosted demo (if any): judge access and step-by-step instructions.
- [ ] Shelf photos (if any) added with the permission note above.
- [x] Voice fixtures are synthetic-generated and labelled; no real voices.
- [x] Payment screenshots are synthetic, fictional banks only; no real screenshots.
- [x] Langfuse tracing off without keys and in CI; redaction tested; only synthetic media attached.
- [ ] Read the submit form and add anything it asks for here.
