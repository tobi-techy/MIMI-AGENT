# Connectors: shop tools with consent (RAI-277)

Shops that already keep a spreadsheet or use a POS link it once, with
consent, so Mimi reads their stock and sales automatically and stops
asking weekly questions. Composio is the transport behind Mimi's own
interface in `mimi/connectors/`: it does OAuth, token storage and
refresh, and the consent screen. Approval, idempotency, audit and undo
stay in Mimi code. The model never calls Composio directly; only
allowlisted actions are wrapped.

## The interface

`base.py`: the `Connector` protocol (`read`, `propose_write`,
`apply_write`, `undo`) with typed `Proposal` (before, after, amount in
kobo when money is named, idempotency key) and `WriteRecord` (external
id, approval id, applied and undone times). `composio.py` is the only
module that imports the Composio SDK. `allowlist.py` names every action
Mimi may take; anything else is refused in code. `consent.py` checks a
live grant every read. `sync.py` pulls rows into `stock_counts` with
`source=connector` and logs the run.

## Toolkits and wrapped actions

* `ZOHO_BOOKS` (version `20261026_00`): reads `LIST_ORGANIZATIONS`,
  `LIST_CONTACTS`, `GET_CONTACT`, `LIST_INVOICES`, `GET_INVOICE`,
  `LIST_CUSTOMER_PAYMENTS`, `LIST_INVOICE_PAYMENTS`, `LIST_ITEMS` into
  shops, receivables, payments and SKUs (`zoho/mapping.py`: figures from
  the response only, naira to integer kobo in code, nothing invented).
  Every books read carries `organization_id`: Zoho refuses a books read
  without it, and Composio answers that refusal with an error payload
  rather than an exception. So `zoho/replies.py` checks every reply for
  `code: 0` before a row is mapped; anything else raises with what Zoho
  said. A read that returns nothing means the book holds nothing, never
  that the request was malformed.
  Writes, owner-approved only: `ADD_INVOICE_COMMENT` (undo
  `DELETE_INVOICE_COMMENT`), `UPDATE_CONTACT` (phone or note fields only;
  undo is the same action with the saved before values).
* Customer payments (no create action on Composio): `zoho/payments.py`
  only, over Composio proxy execute on the distributor's existing
  `ZOHO_BOOKS` account (`POST /customerpayments?organization_id=<org>`,
  undo `DELETE /customerpayments/{id}`). Owner-approved only, never
  auto-approved; amount and invoice from Mimi's records (a Proof-verified
  payment); idempotency key in `reference_number` plus a read-back by
  reference before creating; exactly one call per approval (the proxy
  never retries; on timeout read back first). This records a payment the
  shop already made; Mimi still never moves money.
* `ZOHO` CRM: reads `LIST_ACCOUNTS`, `SEARCH_ACCOUNTS`, `LIST_CONTACTS`,
  `SEARCH_CONTACTS`, `GET_RECORD`; write `CREATE_NOTE` (undo
  `DELETE_NOTE`).
* `GOOGLESHEETS`: reads only (`GET_SPREADSHEET_INFO`, `GET_SHEET_NAMES`,
  `VALUES_GET` or `BATCH_GET`). Nightly pull, not triggers. This issue
  never writes to shop sheets.
* Never wrapped: any other `DELETE_*`, `VOID_*`, `WRITE_OFF_*`, bank
  account and bank transaction actions, vendor payments, refunds, and
  `EMAIL_*` / `REMIND_*` (they message shops).
* CSV/Excel (`csv/`, in-house, standard library `csv`; Excel needs
  `openpyxl`): parsed server side; bad files (empty, wrong format,
  10k+ rows) fail with a plain message.
* Mono (`mono/`, stub only): same interface and a fake; the direct
  connector lands later. Terminal and wallet providers stay on forwarded
  alerts and exported reports.

## Consent

Grants are rows (shop, receiver, scope, granted and revoked times):
the distributor (its own products only) on, brands as totals (no shop
name, no money) off by default, everyone else locked. Every read checks
a live grant; revoking takes effect before the next sync. Terminal sales
value is read only with explicit consent and feeds the shop's own stock
estimate only, never credit.

## The connect flow

Our own Zoho and Google OAuth apps as custom auth configs, so the
consent screen says Mimi: connect with
`connected_accounts.initiate(user_id=<tenant>, auth_config_id,
callback_url)`; status and disconnect with `get / list / delete`;
execute with `tools.execute(slug, user_id, connected_account_id,
version=< pinned>, arguments)` (direct execution refuses an unpinned
version). Zoho connections pass the account region (`com`, `eu`, `in`,
`cn`, `au`). Endpoints: `GET /v1/connectors`, `POST
/v1/connectors/{toolkit}/connect`, `GET /v1/connectors/callback`, `GET
/v1/connectors/{connection_id}`, `DELETE /v1/connectors/{connection_id}`
(owner key; demo and offline runs use FakeComposio, never real OAuth).

New tables (alembic `0010`): `connector_accounts` (no tokens),
`connector_sync_runs`, `connector_writes`, `consent_grants`,
`stock_counts`. New env vars: `COMPOSIO_API_KEY`, `MIMI_CONNECTORS`
(`fake` default, `composio`, `off`), `MIMI_COMPOSIO_AUTH_ZOHO_BOOKS`,
`MIMI_COMPOSIO_AUTH_ZOHO`, `MIMI_COMPOSIO_AUTH_GOOGLESHEETS`,
`MIMI_COMPOSIO_CALLBACK_URL`, `MIMI_ZOHO_REGION` (default `com`),
`MIMI_COMPOSIO_VERSION_*`, `MIMI_CONNECTOR_STATE_SECRET`,
`MONO_SECRET_KEY`, `MONO_API_BASE`.

## The demo org and the one real write (beat 3)

For the demo the owner connects his own Zoho Books org and Mimi records
one real payment there on camera. Two settings bound that write, and
both live in the Atlasflow dashboard:

* `MIMI_ZOHO_LIVE_WRITE=1` lets an approved `zoho_payment` write to the
  real org. Off, the write stays in Mimi's own fake books and the
  owner text says so plainly.
* `ZOHO_BOOKS_ORGANIZATION_ID` names the org (demo night: the org id the
  owner gave). When it is set, the write uses it and only it; when it
  is unset, the write refuses rather than guessing an org. A real
  payment into the wrong org is the worst outcome, so there is no
  "first org off the connection" fallback on the write path.
* `MIMI_ZOHO_LIVE_WRITE_MAX` caps how many real writes one tenant may
  make per Lagos day (default 1 when the switch is on; unset never
  means unlimited). Fake and demo writes never count. At the cap the
  approval records a plain refusal and stays visible: nothing was
  written. To make another one the same day, set the cap to 2 and
  approve again.

## Eval

`make connectors-eval` (replay, CI): 30 messy sheets (first suggestion
30/30, SKU precision 31/31, zero invented), Zoho read sync, one write
path with approval and undo, Sheets/CSV import, consent on every read.
`make connectors-eval-live` needs a Zoho Books trial org (optional).
Fixtures from Coral Lane: `scripts/make_connector_fixtures.py`,
`scripts/make_connector_sheet_cases.py`.

## Demo script (hackathon beat)

1. `make api` (offline: FakeComposio). Open Settings > Connectors:
   Zoho Books, Zoho CRM, Google Sheets, CSV, Mono (soon).
2. Connect Zoho: fake redirect, callback, sync preview with counts and
   3 mapped rows.
3. "Record Mama Sola's N500,000 transfer": propose, approve in
   Approvals, shows in Zoho (fixture `zp-001`), then Undo.
4. Sheets import: 3-row preview, columns mapped, weekly question off.
5. Disconnect: revoke row, next sync refused.

## Cost and privacy

Composio Hobby is free for 100k tool calls a month (20k through
Composio-managed apps); a nightly sync is a few dozen calls per
distributor. Zoho's own API limits apply. Composio is a subprocessor
holding Zoho and Google tokens: its DPA is Enterprise only and a zero
data retention option exists (`docs/COMPLIANCE.md` and the DPIA).
Google's spreadsheet scope needs Google app verification before wide
use. Traces: one Langfuse span per connector call and per approval,
phones and emails redacted by `mimi/observe/redact.py`.
