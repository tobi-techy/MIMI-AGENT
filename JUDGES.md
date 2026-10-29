# For judges: Mimi in five minutes

Mimi is a set of WhatsApp agents for FMCG distributors in Nigeria: receipts
after every drop, replies read in any language (typed or as a voice note),
payment screenshots checked in code, stock asked with one tap, credit asks
drafted for the owner. The owner sees and approves everything in a console.
Mimi proposes, code checks, the owner approves. Mimi never moves money.

All demo data is synthetic. Coral Lane Trading, its 40 shops, people, phone
numbers, brands and banks are made up.

## 1. Open the hosted demo

> **Not deployed yet.** The Cloudflare build for the demo console at
> `https://demo.userail.money` is prepared in the console's repository
> (`deploy/demo-console/`), with the API address left as a placeholder;
> nothing is live. Until it is, run it on your machine (section 4).

When it is live:

1. Open `https://demo.userail.money`. No sign-up, no key: the page asks the
   API for a demo session and you get **your own fresh copy** of Coral Lane
   Trading. Nobody else sees what you do. The session lasts an hour; its
   copy is deleted after that.
2. Things to try:
   * Ask Mimi something: "Who owes me the most this week?" Watch the
     thinking line show each step, then the reply.
   * Attach a photo, an invoice PDF or a voice note with **+** or the mic.
     It uploads with a progress line and goes to Mimi as a real turn.
   * Open **Threads**, then a shop's thread: approve or decline a card. A
     card only changes once the API has decided. Approving a credit-limit
     raise is refused on purpose in the demo.
   * Play a voice note on a shop's thread: Mimi's reply, read aloud.
   * Open **Shops** for the book (owed, overdue, invoices, payments) and
     **Credit** for limits and requests.
3. **Reset demo** (top of Mimi's thread) starts a new copy.

## 2. What is new in the hackathon window and what was there before

Window: 26 Oct 2026 12:00 PM ET to 4 Nov 2026 9:00 PM ET.

| | What | Where |
|---|---|---|
| **New, in the window** | Everything in this repository: the agents, harness, router, durable runtime and store, the HTTP API with demo mode, voice notes, payment-screenshot checks, stock, credit, the evals and the synthetic world. Every commit here is inside the window (`python scripts/hackathon_provenance.py`, run in CI) | this repo, details in [`docs/HACKATHON.md`](docs/HACKATHON.md) |
| **New, in the window, outside this repo** | The console's connection to this API: its API client, demo sessions, live threads, approvals, shops and credit, voice-note playback and composer attachments. Written from 29 Oct 2026 on a branch of the console's own repository | console repo, branch `feat/console-live-api` |
| **Before the event** | The console's screens and design (a UI prototype on sample data), the marketing site userail.money, and an earlier Mimi backend. Nothing in this repo imports or calls the earlier backend | separate repositories |

So: the screens you click through are the pre-existing console prototype;
every answer, step, card, figure and voice note in them comes from this
repo's API, built in the window.

## 3. Demo caps and safety

Enforced by the API (`mimi/api/demo.py`, `mimi/api/uploads.py`), before
anything is queued. The naira figures are placeholders the operator sets.

| | |
|---|---|
| Model spend per visitor address | ₦100 a day (`MIMI_DEMO_IP_DAILY_CAP_NAIRA`), counted across all of that address's sessions, reset at midnight Lagos time |
| Model spend for the whole demo | ₦2,000 a day (`MIMI_DEMO_GLOBAL_DAILY_CAP_NAIRA`) |
| Sessions | one hour each (`MIMI_DEMO_SESSION_TTL_S`), 20 per address a day |
| Kill switch | `MIMI_DEMO_KILL=1` refuses every new turn; reads keep working |
| Refused in demo | approving a credit-limit raise or a raise of Mimi's own daily allowance (declining works) |
| Attachments | JPEG, PNG or WebP photos, Ogg, WebM, M4A, MP3 or WAV voice notes, PDFs; 10 MB a file, 30 files and 60 MB a copy; the first bytes must match the type; a turn may only point at the caller's own uploads; deleted with the copy |
| Keys | no key is in the console's JavaScript; the session token lives in the tab's session storage and is sent only as `Authorization: Bearer` (never in a URL, including audio and image loads) |
| Money | Mimi never moves money. A screenshot is never a payment; only the bank's credit verifies one |
| Content | replies are rendered as text: links only to web, mail, phone or console pages; images only over https; raw HTML stays text |

When a cap is reached the console says so in plain words and you can keep
looking around.

## 4. Run it locally, offline, for ₦0

No key: the offline model answers every message with one holding line and
never writes a figure, so nothing is spent. Python 3.11 or 3.12.

```bash
git clone https://github.com/tobi-techy/MIMI-AGENT.git && cd MIMI-AGENT
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

# the whole bar, offline: tests, lint, types and the recorded evals
META_API_KEY= pytest
python -m ruff check . && python -m mypy
META_API_KEY= python -m mimi.api.e2e --no-write     # 10 shops end to end, recorded answers

# the API in demo mode, the way the hosted demo runs it
export DATABASE_URL=sqlite:///./demo.db MIMI_MODE=demo META_API_KEY= \
       MIMI_VOICE_REPLIES=all MIMI_TTS=fake MIMI_MEDIA_DIR=./demo-media
alembic upgrade head && python -m mimi.store seed --if-empty
python -m mimi.api serve --host 127.0.0.1 --port 8000
```

`MIMI_TTS=fake` reads voice replies as a quiet WAV tone, with no speech
service. Then try it with curl:

```bash
TOKEN=$(curl -s -X POST 127.0.0.1:8000/v1/demo/sessions | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s -X POST '127.0.0.1:8000/v1/turns' -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"message_id":"judge-1","source":"owner_message","text":"Who owes me the most?"}'
curl -s 127.0.0.1:8000/v1/threads -H "Authorization: Bearer $TOKEN"
```

The console itself lives in its own repository. With that repository
checked out, `NEXT_PUBLIC_MIMI_API_URL=http://127.0.0.1:8000 npm run
build:console-demo` builds the demo console into `out-console/`; serve that
folder with any static server on localhost (the API allows localhost
origins).

More: [`docs/API.md`](docs/API.md) (every endpoint and the demo mode),
[`docs/RUN.md`](docs/RUN.md), [`README.md`](README.md).
