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
  `0008_thread_inbox`, `0009_demo_sessions`), the engine
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
docker run -p 3000:3000 -e MIMI_API_KEYS=... -e META_API_KEY=... mimi-agent
```

The image migrates, seeds the synthetic world once (`MIMI_SEED_DEMO=0`
turns that off) and serves on `0.0.0.0:$PORT` (default 3000, the port the
hosting platform probes; `make api` still serves on 8000 locally). Its
health check, and the platform's probe, is `GET /` (liveness: no database,
no auth); `GET /v1/health` is the readiness check with the database and
queue. Its default database is SQLite in `/data` (mount a volume); set
`DATABASE_URL` for Postgres.

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
| `MIMI_MODE` | `live` | `live` (bearer keys only) or `demo` (public demo sessions, below) |
| `MIMI_DEMO_IP_DAILY_CAP_NAIRA` | 400 | demo: model spend per visitor address per Africa/Lagos day, across its sessions (about 25 turns; 100 ended a visit after 4 to 8) |
| `MIMI_DEMO_GLOBAL_DAILY_CAP_NAIRA` | 2000 | demo: model spend across every demo session per day |
| `MIMI_DEMO_SESSION_TTL_S` | 3600 | demo: how long a session token lasts |
| `MIMI_DEMO_SESSIONS_PER_IP` | 20 | demo: new sessions per visitor address per day |
| `MIMI_DEMO_KILL` | off | demo: `1` refuses every new turn and every new or reset session (503, same body as a refused turn); reads keep working |
| `MIMI_DEMO_CLIENT_IP_HEADER` | `cf-connecting-ip` | demo: the one header the edge sets to the visitor's address (Cloudflare, in front of Atlasflow, writes `CF-Connecting-IP` and overwrites a client's own); read only from a trusted peer; `none` turns it off. `X-Forwarded-For` is refused here |
| `MIMI_DEMO_TRUSTED_PROXIES` | private, loopback and CGNAT ranges | demo: comma-separated CIDRs (the platform edge) whose requests may carry that header; `*` trusts any peer, only when the app is reachable through the edge alone |
| `MIMI_DEMO_TRUST_FORWARDED` | ignored | removed 30 Oct 2026: `X-Forwarded-For` is never read (a made-up one bought a fresh allowance) |
| `MIMI_DEMO_IP_SALT` | dev salt | demo: key for the address digests; required in production |
| `MIMI_MEDIA_DIR` | none | where the helpers read a message's image by its `ref` (`local:<path>`), checked against its `sha256`; the simulator and the e2e eval use it. Without it no image is read |

Production (`MIMI_ENV=production`) refuses to start without
`MIMI_PHONE_PSEUDONYM_KEY`, without `MIMI_API_KEYS`, without `META_API_KEY`
(it never runs the offline model), on SQLite (there is no override), and with
either of the Makefile's local demo keys in `MIMI_API_KEYS`. `make api` and
`make api-seed` with `MIMI_ENV=production` fill in no defaults: they stop
unless `DATABASE_URL` and `MIMI_API_KEYS` are set. With `MIMI_MODE=demo`,
production needs `MIMI_DEMO_IP_SALT` instead of `MIMI_API_KEYS` (visitors use
session tokens the API issues) and still refuses SQLite, the offline model
and the Makefile's demo keys.

## Demo mode (`MIMI_MODE=demo`)

A public console on synthetic data (`mimi/api/demo.py`):

* `POST /v1/demo/sessions` (no auth) seeds a fresh copy of the fictional
  Coral Lane Trading world under a new tenant (`demo-<hex>`, the same seed as
  `python -m mimi.store seed --tenant-id`) and returns a session `token`
  (send it as `Authorization: Bearer`), `expires_at` and the caps. The token
  is an owner key for that copy only; the store keeps its SHA-256 and a keyed
  digest of the visitor address, never either in plain. No key is ever
  baked into the console's JavaScript.
* `POST /v1/demo/reset` ends the caller's session and starts a fresh copy.
* While `MIMI_DEMO_KILL=1`, both answer 503 `{"detail": "the demo is paused right now; you can
  still look around"}` (the body a refused turn gets) and seed nothing; a
  refused reset leaves the caller's session as it was.
* A token past its TTL is 401 (`demo session expired`). Expired copies are
  deleted when a new session starts (after a 10 minute grace); their spend
  is kept per address and day so the caps still count it.
* Before a turn or event is queued: `MIMI_DEMO_KILL` (503), the global daily
  cap, then the per-address daily cap (429). Nothing is written when refused.
  Each copy's own daily cap is the per-address cap, so a run that goes over
  mid-turn parks on the loop's daily cap like any distributor.
* Approving a credit review, a credit limit change or a raise of the daily
  allowance is refused (403); declining works.
* `GET /v1/me` shows the session's expiry, today's spend against the caps,
  `resets_at` and `ip_source` (`header` when the visitor address came from
  the edge's `CF-Connecting-IP`, `peer` when it is the socket address; never
  the address itself). After a deploy, `ip_source` must read `header`, or
  every visitor shares one allowance.
* The visitor address is never taken from `X-Forwarded-For` (or `X-Real-IP`):
  a client can write those. Only `MIMI_DEMO_CLIENT_IP_HEADER` from a peer in
  `MIMI_DEMO_TRUSTED_PROXIES`, else the socket peer.

### Demo limits: the refusal the console shows

Every demo refusal (429 for a cap or too many sessions, 503 when paused)
answers one shape. `detail` is kept for older clients; `demo_limit` is what
the console shows (OpenAPI `DemoLimitError`):

**The naira caps do not bound a browser task.** A computer task spends browser
minutes, not model tokens, so it writes no kobo into `demo_usage`, which is
what `gate` sums: adding the gate to the two computer routes gives the kill
switch and nothing else, and a run that costs no naira will never push a
visitor over a naira cap. The count of tasks is what bounds them, checked where
a task is created (three a session, eight a visitor address a day), which is
why `POST /v1/computer/tasks` answers `session_task_cap` or `visitor_task_cap`
rather than a spend cap. `POST /v1/computer/tasks/{id}/approve` is gated but
never counted, so resuming a paused task does not spend the allowance twice.

| Field | Type | What |
|---|---|---|
| `code` | string | `visitor_daily_cap`, `global_daily_cap`, `paused`, `too_many_sessions`, `session_task_cap`, `visitor_task_cap` |
| `scope` | string | `visitor` (this address), `global` (every visitor), `demo` (paused) |
| `title` | string | a short heading: `You have used today's demo allowance` |
| `message` | string | one plain sentence to show under it |
| `spent_kobo`, `cap_kobo` | int or null | today's spend and the cap (null when not a spend cap) |
| `spent_naira`, `cap_naira` | string or null | the same as `₦400` |
| `resets_at` | string | next midnight Africa/Lagos, ISO 8601 (`2026-10-31T00:00:00+01:00`); empty when paused |
| `retry` | string | `tomorrow` or `later` |

```json
{
  "detail": "you have used today's demo allowance; it resets at midnight Lagos time",
  "demo_limit": {
    "code": "visitor_daily_cap", "scope": "visitor",
    "title": "You have used today's demo allowance",
    "message": "You have used today's demo allowance; it resets at midnight Lagos time.",
    "spent_kobo": 40120, "cap_kobo": 40000, "spent_naira": "₦401.20", "cap_naira": "₦400",
    "resets_at": "2026-10-31T00:00:00+01:00", "retry": "tomorrow"
  }
}
```

A run that crosses the cap mid-turn is not refused: it parks on the loop's
daily cap (the owner gets the usual holding note) and the next turn gets the
refusal above.
* Console attachments (`mimi/api/uploads.py`): the demo console has no
  WhatsApp channel to keep media, so the API stands in for one.
  `POST /v1/media` takes one file as the raw request body with its
  `Content-Type` (JPEG, PNG or WebP photo; Ogg, WebM, M4A, MP3 or WAV voice
  note; PDF), at most 10 MB, and answers `ref`, `sha256`, `mime`, `kind` and
  `size` to send as the turn's `media`. The first bytes must be the type the
  file claims; at most 30 files and 60 MB per copy. A turn's `local:` `media.ref` must
  be one of the caller's own uploads with its hash (422 otherwise), so a
  visitor can never point Mimi at another copy's file or the eval fixtures.
  `GET /v1/messages/{message_id}/media` reads an attachment back (the owner's
  own `in:` messages only). Uploads live under `MIMI_MEDIA_DIR` (a temporary
  directory when unset) and go when their copy is swept. The upload passes
  the same caps gate as a turn; it calls no model.

In live mode the demo routes are 404 and nothing above applies.

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
| `POST /v1/turns` | any | One inbound message: `message_id` (the dedupe key), `source` (`shop_message`, `owner_message`, `rep_message`), `phone`, `text`, `button_id`, `media` (`kind`, `ref`, `mime`, `sha256`, size, `caption`, `transcript`), `received_at`, `simulated`, `assign_to` (owner only: the helper to hand it to, [HANDOFFS.md](HANDOFFS.md)). Waits for the replies (200) or answers 202 with the turn id; `?wait=false` queues only. The same `message_id` again returns the same turn and replies with `duplicate: true` |
| `POST /v1/events` | any | One system event from Go or the scheduler: `event_id` (the dedupe key), `kind` (`webhook` or `routine`), `name` (`delivery_completed`, `stock.weekly_check`, ...), `shop_id` (empty for the distributor), `received_at`, `simulated`. Queued as a turn on the shop's thread and routed by rule (202 with the turn id). The words the helper sees are written by code from the kind, name and shop; an event carries no free text. Another distributor's shop is 404 |
| `GET /v1/turns/{turn_id}` | any | State, status, route, run id (the trace id) and replies; plus the handoff contract (`handoff`, `handback`, `card`, `chips`, `task_status`, [HANDOFFS.md](HANDOFFS.md)) |
| `GET /v1/tasks` | owner | Each helper task (a turn Mimi handed to a helper), newest first, with its status (`working`, `needs_you`, `done`, `handed_back`, `failed`); `thread_id`, `helper`, `status`, `limit` ([HANDOFFS.md](HANDOFFS.md)) |
| `GET /v1/threads` | owner | Threads, newest first: kind, shop, last message, pending approvals, and for the console `helper` / `helper_name` (newest routed turn's helper), `status` (`needs_you`: a pending approval or a turn waiting on a person, an ask or a park; `running`: a turn queued or running; `done`) and a code-written `title` (never shop text). `limit`, `offset`, `kind` |
| `GET /v1/threads/{thread_id}` | owner | Messages (each with `voice`: the reply has a voice note at `/v1/messages/{id}/voice`, and `media`: in demo, the owner's console attachment at `/v1/messages/{id}/media`), reasoning steps (routing and every harness step: tool, tier, short line, model, cost, time; never the model's private text), turns, router state |
| `GET /v1/shops` | owner | Shops with credit limit, outstanding and overdue (kobo and naira, summed in code), phone as last four digits |
| `GET /v1/shops/{shop_id}` | owner | Plus open invoices, payments, promises, the last 10 deliveries with lines, disputes |
| `GET /v1/approvals` | owner | `status=pending` (default), `approved`, `rejected`, `superseded`, `withdrawn`, `all`; each with the tool, preview, payload, the drafted message and `supersedes` / `superseded_by` (RAI-301). `preview` and the draft text are as the owner reads them (names for ids, `₦1,234,500`, below); `preview_raw` is the record as written; `shop_name`; `flags` (`["over_credit_limit"]` when approving the order would put the shop past its limit) and `over_limit_by_kobo`; `handling` (`approve`, or `hand_off`: the owner does it by hand and approving never sends Mimi's draft, RAI-323) |
| `POST /v1/approvals/{id}/approve` | owner | Releases the drafted message, marks the intent approved and wakes the helper on its thread (`approval_answered`, through the router). A money verb is refused (409). Deciding twice is 409 |
| `POST /v1/approvals/{id}/reject` | owner | Nothing is sent; the helper is told |
| `GET /v1/policy` | owner | The rulebook (RAI-323, docs/POLICY.md): each step with its behaviour (`act`, `act_if_preapproved`, `ask`, `hand_off`), default, allowed behaviours and who changed it; built-in stops listed `locked`; `editable` false in demo mode |
| `PUT /v1/policy` | owner | Change rules: `{"rules": [{"action_type", "behaviour"}], "by"}`. 409 for a built-in stop, floor or unknown type (nothing changes); 403 for a demo session or demo mode. Logged with who; in force on the next turn |
| `GET /v1/routines` | owner | The routine rail (4 Nov 2026, [ROUTINES.md](ROUTINES.md)): `{"routines": [{id, name, schedule, paused, paused_reason?, last_run_at, next_run_at, last_finding, last_finding_shop_id?}], "as_of"}`. `name`, `schedule` and `last_finding` are sentences written by code; `last_run_at` and `next_run_at` are ISO 8601 with an explicit zone (Africa/Lagos) and empty `last_run_at` when the routine has never run, with `last_finding` saying so. `paused_reason` is present only when paused. `last_finding_shop_id` is present only when the last finding is about exactly one shop; it is never guessed |
| `POST /v1/routines/{id}/pause`, `POST /v1/routines/{id}/resume` | owner | `{id, name, paused}` plus an optional body `{reason?, by?}`. The switch is a row in the store, so it survives a restart and needs no deploy. `all` (or `*`) is the kill switch: one row stops every routine. Pausing a routine under the kill switch still answers `paused: true` |
| `POST /v1/routines/{id}/run` | owner | Runs one routine now through the tick's own path (same finding, message and recording), for a demo or a check. Answers that routine's row exactly as `GET /v1/routines` reads it after the run. The same Lagos-day claim applies, so a second run today is 409 "It has already run today. It runs again tomorrow at 8:00 AM."; a paused routine is 409 with its reason. Neither sends anything. No model call |
| `GET /v1/credit` | owner | Per-shop limit, outstanding, available, and credit requests from helpers' credit intents (Credit, RAI-271). Each limit row also has `over_limit`, `over_limit_by_kobo`, `over_limit_by_naira` (owes more than its limit, from code) |
| `POST /v1/credit/{shop_id}/limit` | owner | The owner sets a shop's limit: `{amount_ngn, reason?}` (whole naira) answers `{shop_id, old_limit_ngn, new_limit_ngn, change_id}`. Recorded as a `limit_change` step on the shop's thread. 404 for another distributor's shop |
| `POST /v1/credit/changes/{change_id}/undo` | owner | Puts the limit back: `{shop_id, old_limit_ngn, new_limit_ngn, change_id, undone: true}`; a second undo is 409 |
| `POST /v1/approvals/{id}/undo` | owner | An approved `zoho_payment`: Zoho Books' delete path (`result.undo_available` turns false). An approved `credit_over_limit`: the old limit back. An approved `slow_stock`: the plan is off, the shop is told, and a return opened from the pickup that collected nothing is cancelled. An approved `return_credit`: the credit is taken back off the invoices it touched by arithmetic, so it stays right after a bank payment or a morning roll moved them, and the return returns to `inspected` (`docs/RETURNS.md`). 409 for anything else or a second undo |
| `GET /v1/stock/sell-through` | owner | Sales tracking (3 Nov 2026): per shop and product `sold_units` over `sold_over_days` (`sold_method` `counts`: two shop counts, count + delivered - later count; else `estimate`), `sold_value_ngn`, `proof_level` / `proof_grade` / `proof` ("estimated (shop count)"), `units_left` (estimate), `days_of_cover`, `days_to_expiry`, `expires_on`, `status` (`near_expiry`, `slow`, `low`, `ok`) and `status_word`, `watch` (a low-confidence slow reading: hint only), `confidence`, `at_risk_units` / `at_risk_ngn`, `last_count_on`, a plain `note` (names and ₦, no ids). `days` 7 to 90 (28), `shop_id`, `status` filters; top-level `counts` per status, `at_risk_ngn`, `at_risk_text`. Code only |
| `POST /v1/stock/slow-check` | owner | Runs the weekly slow-stock check (also the `slow_stock_check` routine with `MIMI_DEMO_WIRING=1`): opens `slow_stock` approvals, one per shop and per item a week (a rerun opens nothing). `{week, opened, skipped, checked}` |
| `GET /v1/returns` | owner | The returns ledger (3 Nov 2026, `docs/RETURNS.md`), newest first: `status` (`drafted`, `approved`, `collected`, `inspected`, `settled`, `cancelled`) and `shop_id` filters. Each return carries its lines (product, unit, drafted, collected and returned quantities, condition, disposition, and the invoice `batch`, `expires_on` and `expiry_source`), its reason, `value_drafted_kobo`, `value_returned_kobo`, `recovered_kobo`, the owner's `pickup_cost_kobo`, `saving_kobo` and `saving_basis`, its credit fields and its `transitions`. Names and ₦ only, no ids |
| `GET /v1/returns/{return_id}` | owner | One return; 404 for another distributor's |
| `POST /v1/returns` | owner | Opens a draft: `{approval_id}` from an approved `slow_stock` **pickup** (idempotent: the same pickup returns the one draft), or `{shop_id, reason, lines: [{sku, quantity, reason}]}`. Quantities and naira are summed in code. 409 when the pickup was not approved or its option is not a pickup |
| `POST /v1/returns/{id}/approve` | owner | `drafted` to `approved` |
| `POST /v1/returns/{id}/collect` | owner | `approved` to `collected`: the rep collected. `lines: [{line_no, quantity}]` may lower each line (never above the drafted quantity) |
| `POST /v1/returns/{id}/inspect` | owner | `collected` to `inspected`: every line named with `quantity_returned`, `condition` (`resellable`, `damaged`, `expired`) and `disposition` (`resell`, `write_off`, `return_to_supplier`). 409 when more came back than was collected, or a line is missing |
| `POST /v1/returns/{id}/settle` | owner | `inspected` drafts a pending `return_credit` approval: `{by?, amount_kobo?}` (default what came back; 409 above it). Settling again with the same amount hands back the waiting card; a different amount replaces it. Nothing changes until the owner approves it (`POST /v1/approvals/{id}/approve`), and it can be undone like a Zoho payment |
| `POST /v1/returns/{id}/cancel` | owner | `drafted` or `approved` to `cancelled` |
| `GET /v1/returns/settings`, `PUT /v1/returns/settings` | owner | The pickup cost `{pickup_cost_kobo}` a return reports against, so `saving_kobo` rests on a stated figure, not a guess |
| `GET /v1/credit/exposure` | owner | Credit exposure per shop and in total, code only: `outstanding_kobo` and `overdue_kobo` (the money owed), the ageing bands (`current`, `days_1_30`, `days_31_60`, `days_over_60`) with their weights and per-shop `buckets`, the risk-weighted `exposure_kobo` (an assumption, always shown with its weights), `verified_paid_kobo` and `shown_not_counted_kobo` (only bank-verified payments count) and a defined `confidence` (`low`, `medium`, `high`). The `buckets` block defines the boundaries once, so no surface keeps its own copy |
| `GET /v1/mentions` | owner | The console's @ picker: `{mentions: [{kind, id, name, subtitle, mention, assign_to}], total}`. `kind` is `helper`, `shop` or `rep`. `q` matches the start of a name or of any word in it (a leading `@` is ignored); `kinds` is a comma list (`shop`, `helper,rep`), empty for all; `limit` 1 to 200 (50). Helper rows carry `mention` (`@Collector`) and `assign_to` for `POST /v1/turns`; shop rows' `subtitle` is area and rep, rep rows' is their areas. A channel key is 403 |
| `GET /v1/connectors` | owner | Connector toolkits (Zoho Books, Zoho CRM, Google Sheets, CSV/Excel, Mono soon) with their reads, writes and connections (RAI-277; `docs/CONNECTORS.md`) |
| `POST /v1/connectors/{toolkit}/connect` | owner | Start the connect flow: body `{region}` for Zoho (`com`, `eu`, `in`, `cn`, `au`); returns `redirect_url` and a single-use `state`. Demo and offline runs return a fake redirect, never real OAuth |
| `GET /v1/connectors/callback` | owner | Composio returns here: checks the state (expired or reused links get 422 and a WhatsApp nudge for a new link), reads the connected account, sends the owner back to Settings > Connectors |
| `GET /v1/connectors/{connection_id}` | owner | Status plus a sync preview (counts and the first 3 mapped rows) |
| `DELETE /v1/connectors/{connection_id}` | owner | Deletes the Composio connected account and writes a revoke row before the next sync |
| `GET /v1/messages/{message_id}/voice` | any | The reply's voice note (RAI-290): its own text read aloud, made now and never stored (`audio/ogg`, or `audio/wav` from the offline speaker). 404 when the reply has no `voice` block |
| `GET /v1/health` | none | Database, model mode (`live` or `offline`), queue depth, version |
| `GET /` | none | Liveness for the platform probe: `{"status": "ok"}`, no database |
| `GET /v1/me` | any | The caller's tenant, role and mode (`live` or `demo`); in demo, the session's expiry and today's spend against the caps |
| `POST /v1/demo/sessions`, `POST /v1/demo/reset` | none, demo session | Demo mode only (404 in live mode) |
| `POST /v1/media`, `GET /v1/messages/{message_id}/media` | owner (demo session) | Demo mode only: keep one console attachment for a turn, and read it back (404 in live mode) |

### Demo wiring (2 Nov 2026, `mimi/api/demo_wiring.py`)

The five demo scenes run end to end in code. The read fields and the new
endpoints are always on; the paths that change what a recorded run does are
behind `MIMI_DEMO_WIRING=1` (set in the Dockerfile, off in CI and `make check`).

* **Reads.** `GET /v1/credit` limit rows and `GET /v1/shops/{id}` `shop` carry
  `credit_limit_ngn`, `outstanding_ngn`, `overdue_ngn` and `overdue_days` (days
  since the oldest overdue invoice fell due). `GET /v1/credit` adds `payments`
  (the newest 50) and each payment row in both carries `amount_ngn`,
  `proof_level` (stored: `verified`, `likely`, `estimated`), `proof_grade` (the
  demo's words: `verified`, `estimated`, `unverified`) and its shop's
  `overdue_days` / `overdue_ngn`.
* **Approvals** carry `kind` (`zoho_payment`, `credit_over_limit`, or the tool
  name), `blocked` (a pending `credit_over_limit`) and `result` (what code did on
  approve).
* **Morning brief.** `POST /v1/events {kind: routine, name: morning_brief}` (or
  `mimi.morning_brief`): code writes the top three overdue shops by name and ₦
  on the owner's thread. No model call.
* **Screenshot to verified.** A screenshot Proof records as likely is kept as a
  payment at level `likely`. `POST /v1/events {kind: webhook, name: bank_credit,
  shop_id, amount_ngn?}` matches the newest likely payment (same amount when
  given), stamps it `verified` with a bank witness, takes it off the invoice,
  thanks the shop, tells the owner and opens a `zoho_payment` approval
  (`payload`: `shop_name`, `amount_ngn`, `invoice_number`, `reference_number`).
  No match: the owner is told and nothing changes. No model call.
* **Zoho write.** Approving a `zoho_payment` records it through
  `mimi/connectors/zoho/payments.py` (read back by reference, then one create);
  `result` is `{zoho_payment_id, undo_available: true, demo}`. A real write
  first resolves the shop to its Zoho contact and to the seeded invoice
  `MZ-<shop_id>` (`mimi.api.zoho_match`), and refuses with its reason when
  either is missing or the invoice owes less than the payment applies. The
  `reference_number` carries the approval id (`mimi-apr_<digest>`), so a
  payment can be traced to the approval that made it and taken back out by
  `scripts/seed_demo_zoho.py --reset`, which deletes only those and only
  invoices the seed wrote. In demo mode, or
  without an active Zoho Books connection and `ZOHO_BOOKS_ORGANIZATION_ID`, the
  transport is `StoreZoho`: a deterministic fake that keeps its payments in
  `artifacts` and never calls Zoho. Mimi never moves money.
* **Over the limit.** A shop order queued over its limit also opens a
  `credit_over_limit` approval with `order_ngn`, `limit_ngn`, `owed_ngn`,
  `suggested_limit_ngn` (the shop's owed plus the order, rounded up to ₦50,000)
  and code-written `reasons` (owed against the limit, bank-verified payments
  and amount paid over 90 days, what is overdue). Approving sets the suggested
  limit (undoable); the order's own approval is still the owner's call.
* **Shop text** (behind the switch: the e2e's recorded shop personas read
  Mimi's replies as recorded). What a shop reads (turn `replies` on `shop_whatsapp`, shop
  messages in a thread read) passes `style.shop_text`: `₦` with commas and no
  internal ids ("Thank you. Your delivery is confirmed."; the receipt's
  `Ref dlv-..., 2026-10-26` line reads `Delivered 26 Oct 2026`). The stored
  record, and everything a model reads, keeps what was gated.

### Slow stock and sell-through (3 Nov 2026, `mimi/api/sales.py`)

Code only; see [STOCK.md](STOCK.md#slow-stock-and-near-expiry-sales-tracking-3-nov-2026).

* **`slow_stock` approvals** (`kind: "slow_stock"`). `payload`: `shop_name`,
  `item`, `slow_kind` (`near_expiry` or `overstock`), `expires_on`,
  `confidence`, `expiry_band`, `cover_band`, `reason` (plain, "about 2 kegs of
  Palmside Vegetable Oil 5L may not sell before they expire on 5 Nov."),
  `options` (each `action` `move` / `pickup` / `promo`, `quantity`, `unit`,
  `unit_price_kobo`, `value_kobo`, `value_ngn`, `to_shop_name` for a move,
  `why`, `text` for the owner, `shop_message` the shop gets on approval),
  `recommended`, `chosen`, `moves_stock: false`, `moves_money: false`,
  `result`.
* **Approve** with `{"option": "pickup"}` (default the recommended option; 409
  when not one of its options): the shop gets `shop_message` once on its
  thread, the owner thread a line, and `result` is
  `{option, shop_told: true, undo_available: true, decided_at}`. No follow-up
  turn, no model. **Reject**: nothing is sent. **Undo**
  (`POST /v1/approvals/{id}/undo`): the shop is told to ignore it,
  `result.undo_available` turns false; a second undo is 409.
* **`GET /v1/shops/{id}`** delivery lines carry `expires_on` and `batch`.

Owner-facing text is rewritten by `mimi.api.display` (30 Oct 2026 run 2):
`shop-001` and `rep-03` become the shop's and rep's names from the book, an
approvals, turn or run id (`apr_...`) is never shown, and `N50600`,
`N50,600`, `N50,600.00` become `₦50,600`. This covers owner console replies,
approval previews and drafts, thread messages and the "The owner approved
..." system line. Ids stay in records, payloads and everything a model reads.

Replies come back with `status`: `ready` (hand it to the channel now) or
`pending_approval` (it waits in approvals). A `ready` WhatsApp reply to a
shop that sent a voice note and opted in with `VOICE ON` also carries
`voice` (`script`, `voice`, `language`, and the `amounts_kobo` and `days`
code read back from the script, which are the text's): send the text, then
the audio from `GET /v1/messages/{message_id}/voice`. Off unless
`MIMI_VOICE_REPLIES` names the distributor (`docs/VOICE.md`). Sending them on WhatsApp is the
webhook adapter's job (the Meta app and test number exist; the send wiring
is RAI-266's remaining piece).

### Scheduled routines (4 Nov 2026, `mimi/api/routines.py`)

Nothing used to run unless someone asked. Now two known routines fire on
their own schedule in Africa/Lagos, from one daemon thread inside the API
process (`MIMI_ROUTINES=1`, set in the Dockerfile; off in CI). Both are code
over the book: **no model call and no kobo spent**. See
[ROUTINES.md](ROUTINES.md).

* **Morning briefing** (`morning_brief`, every day at 8:00 AM): the single
  most overdue shop by name, days and ₦, on the owner's thread
  (`kind: "morning_brief"`), with the fuller brief behind it.
* **Aging scan** (`aging_scan`, every day at 8:30 AM): shops whose oldest
  money has passed 60 days, and transfer alerts that never matched a payment
  (`kind: "aging_scan"`). It reports only; nothing on the book changes.
* **Once per Lagos day.** The claim is a row in `routine_runs` whose primary
  key is (tenant, routine, day). A restart or a second API process loses the
  insert and does nothing, so a routine can never fire twice on a day. A run
  that dies half way is not repeated that day either.
* **The owner can run one now.** `POST /v1/routines/{id}/run` goes through the
  tick's own path and answers the routine's row after the run. It takes the
  same day's claim, so a second run today, or the scheduled tick after a
  manual run, is refused with a sentence and sends nothing. A paused routine
  is refused with its reason.
* **A late start catches up.** A tick at 9:00 still sends the 8:00 brief,
  until 4 hours after the schedule (cut at Lagos midnight). The window is per
  routine, so the 8:30 scan stays open half an hour longer than the 8:00
  brief. After the window, a routine that has run before **and has not run
  today** is recorded as missed once with words that state only that its time
  passed; a routine that already ran today keeps its finding and stamp, and a
  routine with no history is not a miss and its rail stays at "not run yet".
  Neither borrows the current time or invents a cause.
* **Pause is a row.** `POST /v1/routines/{id}/pause` sets it, `resume` clears
  it; both survive a restart. `all` is the kill switch: one row pauses every
  routine without a deploy. A paused routine records that it skipped the day
  and why. The rail is empty, not "It has not run yet.", until a routine really
  has: the empty timestamp says so once.
* **One thread per process, one process per container.** `MIMI_WORKERS` adds
  turn threads inside the process, not processes, so they share the one
  scheduler. Several processes are still safe by the claim, but they each
  tick; set `MIMI_ROUTINES=0` on a process that should not.
* **No connection is held.** A tick opens a short-lived connection, does its
  reads and writes, and closes it before waiting again; an idle scheduler
  holds nothing on the pool.
* **The demo kill switch stops it.** `MIMI_DEMO_KILL=1` starts no scheduler,
  so routines stop with turns and sessions. `MIMI_ROUTINES=0` disables them
  entirely in a process.

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

### Two open asks on one thread (RAI-272)

A delivery receipt ("Reply YES to confirm") and a correction question ("Reply
YES or NO", about a pending restock draft or credit request) can both be
open on one shop thread. A bare YES or NO fits both, so it binds to neither
(`Agent._two_asks`, phrase rules in `mimi/harness/steer.py`):

* Code asks one short question: "Yes to the delivery receipt or the order
  change?" (or "the credit change", or the Pidgin form). Both asks stay
  open. Turn status `steer_which`.
* The shop's answer goes to that ask. "The delivery": Order reads the
  receipt reply, with the shop's YES joined to the turn's batch so the
  reader sees the shop's own words; the correction stays open. "The
  change": the correction question's YES or NO path; the receipt stays open.
* Still not clear ("both", another bare YES, "I no understand"): the owner
  gets a note and the shop is told the owner will check. Never a second
  question, never a guess, never consent. Turn status `steer_which_owner`.
* A yes or no whose own words say which ("yes the goods complete", "yes make
  am 300k") goes straight to that ask.

Held-out set: `eval/which/cases.jsonl`, 40 cases written before the fix.
`make which-eval` (offline, code only): 40/40, 0 ambiguous replies bound; the
old code scored 10/40 with 29 bound.

### The daily naira cap parks jobs (RAI-272)

When the distributor's daily model allowance is used up, the loop stops the
run before its next model call (`stopped_budget`, "daily naira cap
reached"). The API parks the job instead of finishing it
(`Agent._park`, one compare-and-set):

* turn state `parked`, status `parked_budget`, with its loop state kept;
* the shop (or rep) gets one holding reply written by code, with no figure;
* the owner gets the loop's note once a day, and one approvals item
  `raise_daily_allowance` per day and cap (approve tier; the proposed new
  cap is double the old one, at least N500 more, computed in code).

Approving it raises the cap and puts every parked job back as a lapsed
lease, so the next worker takes it over from its checkpoint and finishes it
(`resume_parked`). Rejecting it leaves the jobs parked. Nothing is dropped
and nothing moves money: the allowance is Mimi's own model spend.

### Brand: the Promo Check helper (RAI-272)

A minimal, read-only helper behind `brand/promo-info` and
`brand/brand-email` (`mimi/brand/tools.py`):

| Tool | Tier | What it does |
|---|---|---|
| `get_brand_promos` | auto | The promos running today (Africa/Lagos) from a fixed synthetic list of the fictional brands, with the shop reply written by code from those rows |
| `draft_brand_email` | approve (always) | An email to a brand, kept as a draft in the owner's approvals. Never sent: there is no email connector, and approving only marks it approved for the owner |

Brand never promises a discount or a price and never quotes a naira figure
in a draft.

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

The returns ledger adds `returns`, `return_lines`, `return_events` (one row
per lifecycle transition), `credit_notes` (every applied settlement credit,
with the invoice applications it used, so an undo restores exactly what it
changed) and `returns_settings` (the owner's pickup cost). Alembic `0016`.

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
