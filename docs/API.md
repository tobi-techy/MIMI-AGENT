# The agent API and the durable store (RAI-266)

The live Mimi agent behind HTTP, so the distributor console and a phone or
WhatsApp webhook can talk to it. Built in the hackathon window (28 Oct 2026),
as part of RAI-272 (worker-kill requirement) and RAI-273 (console wiring).
The console itself is pre-existing work in a separate private repository;
nothing in this repository imports it or is imported by it.

All demo data is synthetic (Coral Lane Trading is fictional). The model
calls are real when `META_API_KEY` is set.

```
console / WhatsApp webhook
        |  HTTPS, bearer key per distributor
        v
  mimi.api (FastAPI)  ---- POST /v1/turns ---->  store: inbound_messages + turns (one txn)
        |                                              |
        |  claim (lease, compare-and-set)              v
        +---------------------------------->  worker: router -> context -> harness loop
                                                       |  checkpoint after every step
                                                       v
                                  one txn: intents, approvals, replies, history, done
```

* `mimi/api/`: the app (`app.py`, the only module in `mimi` that imports a web
  framework), the agent (`agent.py`), settings (`config.py`), console reads
  (`reads.py`), the helpers it hires (`helpers.py`) and the no-key model
  (`offline.py`).
* `mimi/store/`: tables (`schema.py`, alembic `0006_api_store`, `0007_tenant_days`,
  `0008_thread_inbox`), the engine
  (`db.py`), `SqlGo` (`book.py`, the same ports as `FakeGo`), the turn queue
  (`queue.py`), the context builder's stores (`context_stores.py`) and the
  seed (`seed.py`).
* OpenAPI: [`docs/api/openapi.json`](api/openapi.json) (a test fails when it
  is stale; regenerate with `python -m mimi.api openapi`).

## Run it

```bash
pip install -e '.[dev]'
make api                 # migrate, seed the synthetic world once, serve on :8000
make api-worker          # an extra worker process (optional)
```

`make api` is:

```bash
export DATABASE_URL=sqlite:///./mimi.db            # or postgresql://... (pip install -e '.[postgres]')
export MIMI_API_KEYS="coral-lane:owner:<owner key>,coral-lane:channel:<channel key>"  # pragma: allowlist secret
alembic upgrade head
python -m mimi.store seed --if-empty              # synthetic Coral Lane Trading
python -m mimi.api serve --port 8000              # MIMI_WORKERS worker threads inside (default 1)
```

Without `MIMI_API_KEYS` the Makefile sets two local-only demo keys
(`coral-lane:owner:local-owner-key-0001` and
`coral-lane:channel:local-channel-key-001`); never use those anywhere else.
Without `META_API_KEY` the agent runs the offline model, which answers every
person with one fixed holding line and never writes a figure.

```bash
CHANNEL=local-channel-key-001 OWNER=local-owner-key-0001   # the Makefile's local demo keys
curl -s 127.0.0.1:8000/v1/health
curl -s -X POST 127.0.0.1:8000/v1/turns \
  -H "Authorization: Bearer $CHANNEL" -H 'Content-Type: application/json' \
  -d '{"message_id":"wamid.demo1","phone":"+2340000000100","text":"abeg wetin I owe now?"}'
curl -s 127.0.0.1:8000/v1/threads -H "Authorization: Bearer $OWNER"
```

Docker:

```bash
docker build -t mimi-agent .
docker run -p 8000:8000 -e MIMI_API_KEYS=... -e META_API_KEY=... mimi-agent
```

The image migrates, seeds the synthetic world once (`MIMI_SEED_DEMO=0`
turns that off) and serves on `$PORT` (default 8000). Its default database is
SQLite in `/data` (mount a volume); set `DATABASE_URL` for Postgres.

## Settings

| Variable | Default | What |
|---|---|---|
| `MIMI_ENV` | `dev` | `production` turns on the start-up checks below |
| `DATABASE_URL` (or `MIMI_DB_URL`) | `sqlite:///./mimi.db` | the store; `postgres://` is accepted |
| `MIMI_API_KEYS` | none | `<tenant>:<role>:<key>`, comma separated; `<key>` may be `sha256:<hex>` |
| `MIMI_CORS_ORIGINS` | `https://console.userail.money` | extra allowed origins; `http://localhost:*` and `http://127.0.0.1:*` are always allowed |
| `MIMI_TURN_WAIT_S` | 25 | how long `POST /v1/turns` waits before answering 202 |
| `MIMI_WORKERS` | 1 | worker threads inside the API process (0 with separate workers) |
| `MIMI_LEASE_S` | 30 | turn lease; renewed every third of it |
| `META_API_KEY`, `META_API_BASE`, `MIMI_MODEL` | none, `https://api.meta.ai/v1`, `muse-spark-1.3` | Muse Spark for the harness and the router's last layer |
| `MIMI_PHONE_PSEUDONYM_KEY` | dev key | required in production (thread ids for unknown numbers) |
| `MIMI_RELEASE` | | shown by `/v1/health` |
| `MIMI_MEDIA_DIR` | none | where the helpers read a message's image by its `ref` (`local:<path>`), checked against its `sha256`; the simulator and the e2e eval use it. Without it no image is read |

Production (`MIMI_ENV=production`) refuses to start without
`MIMI_PHONE_PSEUDONYM_KEY`, without `MIMI_API_KEYS`, without `META_API_KEY`
(it never runs the offline model) and on SQLite unless
`MIMI_ALLOW_SQLITE_IN_PRODUCTION=1`.

## Auth and tenancy

Each bearer key belongs to one distributor (tenant) and one role:

* `owner`: the console. Every read, and approve or reject.
* `channel`: the WhatsApp webhook, Go's webhooks and the simulator. Only
  `POST /v1/turns`, `POST /v1/events` and `GET /v1/turns/{id}`.

The tenant comes from the key, never from the body (a body `tenant_id` that
differs is a 403). Every query filters on it and every key in the store
starts with `tenant_id`, so another distributor's shop, thread, turn or
approval reads as 404. Keys are compared by SHA-256 in constant time; give
`sha256:<hex>` to keep the plain key off the API host.

## Endpoints

| Method and path | Role | What |
|---|---|---|
| `POST /v1/turns` | any | One inbound message: `message_id` (the dedupe key), `source` (`shop_message`, `owner_message`, `rep_message`), `phone`, `text`, `button_id`, `media` (`kind`, `ref`, `mime`, `sha256`, size, `caption`, `transcript`), `received_at`, `simulated`. Waits for the replies (200) or answers 202 with the turn id; `?wait=false` queues only. The same `message_id` again returns the same turn and replies with `duplicate: true` |
| `POST /v1/events` | any | One system event from Go or the scheduler: `event_id` (the dedupe key), `kind` (`webhook` or `routine`), `name` (`delivery_completed`, `stock.weekly_check`, ...), `shop_id` (empty for the distributor), `received_at`, `simulated`. Queued as a turn on the shop's thread and routed by rule (202 with the turn id). The words the helper sees are written by code from the kind, name and shop; an event carries no free text. Another distributor's shop is 404 |
| `GET /v1/turns/{turn_id}` | any | State, status, route, run id (the trace id) and replies |
| `GET /v1/threads` | owner | Threads, newest first: kind, shop, last message, pending approvals. `limit`, `offset`, `kind` |
| `GET /v1/threads/{thread_id}` | owner | Messages, reasoning steps (routing and every harness step: tool, tier, short line, model, cost, time; never the model's private text), turns, router state |
| `GET /v1/shops` | owner | Shops with credit limit, outstanding and overdue (kobo and naira, summed in code), phone as last four digits |
| `GET /v1/shops/{shop_id}` | owner | Plus open invoices, payments, promises, the last 10 deliveries with lines, disputes |
| `GET /v1/approvals` | owner | `status=pending` (default), `approved`, `rejected`, `superseded`, `withdrawn`, `all`; each with the tool, preview, payload, the drafted message and `supersedes` / `superseded_by` (RAI-301) |
| `POST /v1/approvals/{id}/approve` | owner | Releases the drafted message, marks the intent approved and wakes the helper on its thread (`approval_answered`, through the router). A money verb is refused (409). Deciding twice is 409 |
| `POST /v1/approvals/{id}/reject` | owner | Nothing is sent; the helper is told |
| `GET /v1/credit` | owner | Per-shop limit, outstanding, available, and credit requests from helpers' credit intents (Credit, RAI-271) |
| `GET /v1/messages/{message_id}/voice` | any | The reply's voice note (RAI-290): its own text read aloud, made now and never stored (`audio/ogg`, or `audio/wav` from the offline speaker). 404 when the reply has no `voice` block |
| `GET /v1/health` | none | Database, model mode (`live` or `offline`), queue depth, version |

Replies come back with `status`: `ready` (hand it to the channel now) or
`pending_approval` (it waits in approvals). A `ready` WhatsApp reply to a
shop that sent a voice note and opted in with `VOICE ON` also carries
`voice` (`script`, `voice`, `language`, and the `amounts_kobo` and `days`
code read back from the script, which are the text's): send the text, then
the audio from `GET /v1/messages/{message_id}/voice`. Off unless
`MIMI_VOICE_REPLIES` names the distributor (`docs/VOICE.md`). Sending them on WhatsApp is the
webhook adapter's job (the Meta app and test number exist; the send wiring
is RAI-266's remaining piece).

## Helpers hired (RAI-272)

The router hands each turn to one helper on the harness
(`mimi/api/helpers.py`). Each helper's tools read the book through `SqlGo`
and write only intents Go applies:

| Helper | Skills | Tools (tier) |
|---|---|---|
| Collector | what a shop owes, reminders, promises | its 13 tools (RAI-279) |
| Order | `order/drop-confirmation` | `get_open_deliveries`, `read_receipt_reply` (auto); `send_delivery_receipt`, `record_delivery_consent`, `flag_delivery_to_owner`, `ask_receipt_again` (notify) |
| Proof | `proof/fake-alert-check` | `check_payment_screenshot` (auto), `record_payment_proof` (notify, level likely, never money), `flag_payment_proof` (approve, always) |
| Stock | `stock/stock-count` | `get_stock_estimates`, `read_stock_answer` (auto); `ask_stock_count`, `record_stock_count` (notify); `draft_restock`, `revise_restock` (approve, always; `places_order: false`) |
| Credit | `credit/credit-request` | `get_credit_file`, `count_shelf_photo` (SAM 3.1), `prepare_credit_review` (auto); `ask_for_shelf_photo` (notify); `submit_credit_review` (approve, always; never a limit change) |
| Mimi | `mimi/chat`, `mimi/owner-brief` | `send_to_user` only. The owner brief has no data in this build, so it says the daily brief is not ready yet and gives no figures |

Two rules came from the e2e run:

* An approve-tier intent whose shop reply does not depend on the owner
  (`flag_payment_proof`, `draft_restock`) sends its fixed shop notice at
  once, so a turn that parks for approval still answers the shop, exactly
  once (a model `send_to_user` with the same text is dropped).
* A write that asks the shop something (a photo, a stock tap, a YES) leaves
  the thread waiting on that skill, so the answer routes back by rule.

### Pending-ask binding

An open ask binds the shop's next reply to the helper that asked
(`mimi/harness/binding.py`, wired in `Agent`):

* The ask is stored on the thread with the skill, what answer it expects,
  when it was sent (`asked_at`, with its zone) and what it is about
  (`ask_ref`: the delivery id or the sku). It expires: 48 h for a delivery
  receipt, 24 h for a stock tap or a shelf photo (`binding.ASK_TTL`). An
  expired ask captures nothing; the message routes as a new one and the
  route's `thread_note` says so.
* A fixed short answer (a tap, YES, NO, a number, a photo when a photo was
  asked for) binds by rule, with no model call.
* Any other reply: the router model only classifies it against the ask,
  into a fixed label set (receipt: `yes`, `short`, `damaged`, `wrong_items`,
  `unclear`, `unrelated`). Every label except `unrelated` binds to the
  asking helper in code (route rule `pending_bind:<label>`, layer `rule`).
  `unrelated` releases the message to the normal router only at confidence
  0.6 or more, and the ask stays open for the real answer. A binder model
  error or an empty answer falls back to the phrase rules: release only when
  they clearly point at another helper's skill, otherwise bind.
* The binder never decides what the answer means: Order's reply reader and
  its code guards still decide consent or dispute.
* A turn of the asking helper that answers nothing (no consent, flag,
  count or credit submission, and no new ask) leaves the ask open.
* Belt and braces: when `mimi/escalate-owner` fires on a shop message tied
  to a delivery (its receipt ask was open, or the shop named the delivery
  id), code also records `flag_delivery_to_owner` on that delivery (notify
  tier, reason `escalated`, the shop's words quoted, the same business key
  as Order's own flag, so it is never recorded twice). Never consent, never
  a money move. The route result carries `delivery_flagged`.

### Steering: every part of every message, even mid-task (RAI-301)

Mimi hears everything a shop or the owner sends, in any form, and answers
each thing once (`mimi/harness/steer.py`, `mimi/api/steering.py`, wired in
`Agent`):

* **Parts (code).** A message, or a burst of them, is split into parts: each
  typed sentence, each sentence of a voice-note transcript (split where a
  new clause starts too, since speech-to-text has no full stops), an image
  (a payment screenshot or a photo, with its caption) and a document (with
  its caption). The path is the same for every modality; nothing new reads
  media. A voice note's words are its transcript (`mimi.voice`), a
  screenshot is read by Proof, a shelf photo by the helper that asked for it
  or whose open item it supports, a document by nobody yet.
* **Labels (model).** One Muse Spark call with a strict schema labels each
  part: `current` (what Mimi is handling now, including the answer to its
  open ask), `adds`, `corrects`, `cancels` (with the open task it is about),
  `new` (a separate request), `chat` or `unclear`. The stage runs only when a
  message has two or more parts or the thread has open work; otherwise the
  turn runs exactly as before. A model error or an unparseable answer labels
  everything `current` (no split, nothing changed).
* **Plan (code).** The routed helper answers the `current` parts (its event
  carries only those, and a note says the rest is answered elsewhere). Each
  `new` part becomes its own follow-up turn on the same thread
  (`turns.solo`: never merged), queued right after the turn it came from and
  before any later message. A follow-up split off a message that answered
  the open ask does not bind to that ask; the ask stays open. Every part's
  disposition is on the turn result (`result.steer.plan.dispositions`).
* **Open tasks.** Pending approvals on the thread, items decided in the last
  48 hours, and receipts, payment promises and proofs recorded in the last
  48 hours (`steering.open_tasks`), each with a line written by code, go in
  the newest-message notes so "that order" resolves, and are what a
  correction can be about.
* **Corrections (code order).** Unsure (`corrects` or `cancels` below 0.6, or
  no single task it is about) -> one short question. Money or credit
  (Collector, Credit, Proof, a payment promise) -> one short question. Already
  decided (approved, rejected, applied) -> never edited: the owner gets a
  console note quoting the shop and the shop is told it went to the owner.
  Cancel of a pending item -> withdrawn (`approvals.status = withdrawn`).
  Correction of a pending item -> the owning helper makes a new version
  (`revise_restock` for a restock draft: the quantity must be a number in the
  shop's own words, checked in code, priced and capped by code); code then
  marks the old pending approval `superseded` (`superseded_by`, the new one
  `supersedes`), a compare-and-set on `pending`. A superseded or withdrawn
  item cannot be approved (409). A restock correction with no number asks.
* **The question.** A correction question is written by code and waits 24
  hours for YES or NO (`ThreadState.fix`). YES makes the change through the
  owning helper when it is a pending non-money item or a payment promise,
  and tells the owner otherwise; NO leaves it as it is.

### Messages that arrive while a turn runs (RAI-301)

A message for a thread whose turn is running is written to `thread_inbox`
in the same transaction as its queued turn (alembic `0008_thread_inbox`).
No second turn starts on the thread. At its next step boundary the running
loop reads the inbox (owner rows first, then oldest), labels the parts
against the task in progress (`t0`) and the open tasks, and:

* `current`, `adds`, `corrects`, `unclear` and `chat` parts are **absorbed**:
  appended to the loop as an untrusted note on the newest message ("New
  message on this thread while you were working ... answer it in this
  turn's one reply"). The message's turn is merged into the running one and
  its id joins the batch, so tools read it (a stock answer reader skips it,
  `mid_task`).
* `cancels` of the task in progress, from the person who started it or the
  owner, ends the turn `cancelled_by_sender` with nothing applied and one
  short reply. A shop can never cancel a routine or the owner's work.
* `new` parts, a payment screenshot (only Proof reads one) and anything about
  another open task are **released**: the queued turn runs right after, or a
  follow-up turn for those parts when the rest was absorbed.

Absorbing is one compare-and-set (`TurnQueue.absorb`): the loop state with
the note in it, the merge, the batch and the inbox rows. A worker that lost
its lease writes none of it, and a new worker carries on from the saved
state with the message absorbed exactly once
(`tests/test_steer.py::test_absorbed_message_survives_a_worker_kill_exactly_once`).
A message that lands after the last step is never lost: its queued turn is
claimed next and the claim marks the inbox row `released`.
`MIMI_STEER=0` turns the steer stage and the inbox reading off.

The queue takes a lapsed lease first, then the owner, then shops and reps,
then webhooks and routines; oldest first within each.

## Durability: what survives a crash

* **Dedupe**: `(tenant_id, external_id)` is the primary key of
  `inbound_messages`, written in the same transaction as the turn.
* **One turn per thread**; a burst on one thread is one turn (the oldest
  waiting turn leads and the others join its batch, as in the in-process
  front door). The owner's "stop" ends running turns at the next step.
* **Leases**: a claim is a compare-and-set on the turn's `version` and also
  bumps the thread's `claim_seq`, so two workers never win the same turn or
  two turns of one thread (SQLite: `BEGIN IMMEDIATE`; Postgres: the row
  lock). A worker renews its lease every `MIMI_LEASE_S / 3`; a lease that
  lapses is taken over.
* **Checkpoints**: the route decision is saved before the run and the
  harness loop state after every step, so a new worker carries on from the
  last saved step. Event ids are derived from the run and the step, so a
  re-run step writes each step line once.
* **Exactly once**: the turn completes in one transaction guarded by the
  lease: system events, intents (deduped on their business key), approvals,
  replies, history, thread state and `done`. A worker that lost its lease
  writes none of it. `turns.completions` counts completions and must be 1.
* After three failed attempts a turn is `failed` with the error kept.

`tests/test_worker_kill.py` proves it with real processes: a worker is
SIGKILLed mid-run on eight three-step turns, two new workers start at once,
and every turn ends `done` with `completions == 1`, one reply, one plan
intent, one history entry and each step logged once; the killed turn is
taken over from its checkpoint and finished turns are never run again.

## What the store holds

The book (tenants, reps, shops, SKUs, `deliveries` and `delivery_lines` with
totals added up in code, receivables, payments, bank feed, promises,
disputes, consent), and the agent's state (threads, inbound messages, turns,
attempts, messages, steps, intents, approvals, credit requests, daily spend,
asked-once topics, kill switches, history, pinned prefixes, artifacts).
`SqlGo` reads the book exactly as `FakeGo` does (a test compares every port
on all 40 shops). In production Go owns the book; `SqlGo` keeps the same
ports so a real Go client can replace it.

Phone numbers are stored only where the book needs them (shops, reps, the
owner's numbers) and in a queued message until its turn is done; the API
returns the last four digits only.

## Limits and next steps

* WhatsApp send and the webhook adapter (signature check, media download)
  are not here yet; the API returns the replies for the adapter to send.
* A message mid-run reaches the running turn through the inbox (RAI-301). A
  receipt ask and a correction question open at once on one thread: a bare
  YES binds to the receipt.
* Brand turns still report `no_helper_yet:brand`; the other helpers are
  hired (see "Helpers hired" above).
* One live day per tenant (`tenants.live_date`) stands in for the book's
  clock.
