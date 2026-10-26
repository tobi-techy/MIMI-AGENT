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
| Open source allowed if licences are followed | Every package is listed with its licence in `DEPENDENCIES.md`: MIT, BSD-3-Clause, Apache-2.0, PSF-2.0, MPL-2.0 (certifi, unmodified) and LGPL with exceptions (psycopg2-binary, optional, a dynamically loaded driver, unmodified). None requires releasing our code. |
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
* `META_API_KEY` is read only in `mimi/models/env.py` and passed to the
  client; it is never logged, printed or committed. Database URLs come from
  the shell (`MIMI_DB_URL`), never from a file in the repo.

## Ownership

Copyright (c) 2026 Rail Technology Concepts Ltd. All rights reserved
(`LICENSE`). The source is published for judging; no licence to use, copy,
modify or distribute it is granted without written permission.

## Data

* Fully synthetic world: Coral Lane Trading (fictional distributor), 40
  fictional shops in real Ado-Ekiti areas, 30 invented-brand SKUs, 8 weeks of
  deliveries, payments and bank feed, fixed seed (`WORLD.md`).
* Phones use `+2340000...` (never assigned). Account numbers are fictional and
  masked.
* The receipt-reply eval (`eval/receipts/receipt_replies.jsonl`) is synthetic
  and hand-written; every case carries `"synthetic": true`.
* The recorded model answers are Muse Spark's outputs on those synthetic
  cases.

## Assets

No third-party images, logos or fonts are in this repo. Shelf photos for the
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
* Planned during the window: **muse-voice-transcribe-1.0** for WhatsApp voice
  notes and **SAM 3.1** for shelf-photo segmentation.

## Pre-submit checklist

- [x] Fresh public repo with only the window's work.
- [x] Secrets scans in CI (detect-secrets, gitleaks); no key or DSN in the repo.
- [x] README: synthetic data note, offline and live quickstart, eval command.
- [ ] Demo video opens with one line on what was pre-existing and what was
      built this week.
- [ ] Hosted demo (if any): judge access and step-by-step instructions.
- [ ] Shelf photos (if any) added with the permission note above.
- [ ] Read the submit form and add anything it asks for here.
