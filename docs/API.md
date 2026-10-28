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
* `mimi/store/`: tables (`schema.py`, alembic `0006_api_store`), the engine
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

Production (`MIMI_ENV=production`) refuses to start without
`MIMI_PHONE_PSEUDONYM_KEY`, without `MIMI_API_KEYS`, without `META_API_KEY`
(it never runs the offline model) and on SQLite unless
`MIMI_ALLOW_SQLITE_IN_PRODUCTION=1`.

## Auth and tenancy

Each bearer key belongs to one distributor (tenant) and one role:

* `owner`: the console. Every read, and approve or reject.
* `channel`: the WhatsApp webhook and the simulator. Only `POST /v1/turns`
  and `GET /v1/turns/{id}`.

The tenant comes from the key, never from the body (a body `tenant_id` that
differs is a 403). Every query filters on it and every key in the store
starts with `tenant_id`, so another distributor's shop, thread, turn or
approval reads as 404. Keys are compared by SHA-256 in constant time; give
`sha256:<hex>` to keep the plain key off the API host.

## Endpoints

| Method and path | Role | What |
|---|---|---|
| `POST /v1/turns` | any | One inbound message: `message_id` (the dedupe key), `source` (`shop_message`, `owner_message`, `rep_message`), `phone`, `text`, `button_id`, `media` (`kind`, `ref`, `mime`, `sha256`, size, `caption`, `transcript`), `received_at`, `simulated`. Waits for the replies (200) or answers 202 with the turn id; `?wait=false` queues only. The same `message_id` again returns the same turn and replies with `duplicate: true` |
| `GET /v1/turns/{turn_id}` | any | State, status, route, run id (the trace id) and replies |
| `GET /v1/threads` | owner | Threads, newest first: kind, shop, last message, pending approvals. `limit`, `offset`, `kind` |
| `GET /v1/threads/{thread_id}` | owner | Messages, reasoning steps (routing and every harness step: tool, tier, short line, model, cost, time; never the model's private text), turns, router state |
| `GET /v1/shops` | owner | Shops with credit limit, outstanding and overdue (kobo and naira, summed in code), phone as last four digits |
| `GET /v1/shops/{shop_id}` | owner | Plus open invoices, payments, promises, the last 10 deliveries with lines, disputes |
| `GET /v1/approvals` | owner | `status=pending` (default), `approved`, `rejected`, `all`; each with the tool, preview, payload and the drafted message |
| `POST /v1/approvals/{id}/approve` | owner | Releases the drafted message, marks the intent approved and wakes the helper on its thread (`approval_answered`, through the router). A money verb is refused (409). Deciding twice is 409 |
| `POST /v1/approvals/{id}/reject` | owner | Nothing is sent; the helper is told |
| `GET /v1/credit` | owner | Per-shop limit, outstanding, available, and credit requests from helpers' credit intents (Credit, RAI-271) |
| `GET /v1/health` | none | Database, model mode (`live` or `offline`), queue depth, version |

Replies come back with `status`: `ready` (hand it to the channel now) or
`pending_approval` (it waits in approvals). Sending them on WhatsApp is the
webhook adapter's job (the Meta app and test number exist; the send wiring
is RAI-266's remaining piece).

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
* The owner's mid-run steering (a second owner message reaching a running
  turn) is queued as the next turn instead.
* Only Collector, Proof and a small Mimi helper are hired; order, stock,
  credit and brand turns report `no_helper_yet:<helper>` until their
  helpers move onto the harness (Credit, RAI-271, plugs into
  `mimi/api/helpers.py`).
* One live day per tenant (`tenants.live_date`) stands in for the book's
  clock.
