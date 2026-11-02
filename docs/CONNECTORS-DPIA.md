# Data protection note: connectors (RAI-277)

Short version: shops link their existing tools (Zoho Books, Zoho CRM,
Google Sheets, CSV/Excel, Mono soon) so Mimi reads stock and sales
instead of asking weekly questions. Linking is opt-in, per receiver,
and revocable; revoking works before the next sync. Design:
[`docs/CONNECTORS.md`](CONNECTORS.md).

## What data moves

* **Reads:** contacts, invoices, items, customer payments (Zoho Books);
  accounts, contacts (Zoho CRM); stock and sales rows (Google Sheets or
  CSV/Excel uploads); terminal sales value only (Mono, later). Every
  read checks a live per-receiver consent grant first.
* **Who sees it:** the distributor sees only its own products; brands
  see totals only (no shop name, no money) and only when the shop
  switched them on; everyone else sees nothing. Shop data stays
  private in the distributor's own book.
* **Writes:** owner-approved only (a note on an invoice, a phone or
  note field on a contact, a customer-payment record for a payment the
  shop already made, a note in the CRM). Nothing moves money; recording
  a customer payment is bookkeeping of a transfer that already
  happened, and the payment is read back by reference before and after.

## Subprocessors

* **Composio** holds the shops' Zoho and Google OAuth tokens and runs
  the tool calls. Its DPA is on its Enterprise plan and a zero data
  retention option exists. Access is least-privilege: our own OAuth
  apps as custom auth configs (the consent screen says Mimi), read-only
  sheets scope, pinned tool versions, an allowlist of named actions,
  and one Composio account per connection that we delete on disconnect.
* **Google** (Sheets): the spreadsheet scope needs Google app
  verification before wide use; the consent screen names Mimi and the
  read-only scope.

## Minimisation and retention

* No tokens are stored in Mimi's database (connection ids and run
  metadata only). Nightly sync writes mapped rows into `stock_counts`
  with `source=connector`; a run log keeps counts and errors.
* Traces redact phones, long digit runs, keys and tokens
  (`mimi/observe/redact.py`); one span per connector call and per
  approval.
* Before launch: confirm the Composio plan covers the DPA and zero
  data retention, and finish Google verification for the sheets scope.
