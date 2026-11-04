# Returns and exposure contract (frozen, feat/returns-and-exposure)

The console builds against these strings. Additive only: no existing path,
field or status changes. All money is integer kobo except the `*_ngn` and
`*_naira` display fields.

## 1. Returns

Paths:

- `POST   /v1/returns` -> 201 ReturnOut
- `GET    /v1/returns` -> ReturnList (query `status=`, `shop_id=`)
- `GET    /v1/returns/{return_id}` -> ReturnOut (404 unknown)
- `POST   /v1/returns/{return_id}/approve` -> ReturnOut
- `POST   /v1/returns/{return_id}/collect` -> ReturnOut
- `POST   /v1/returns/{return_id}/inspect` -> ReturnOut
- `POST   /v1/returns/{return_id}/settle` -> ReturnOut (opens the `return_credit` approval)
- `POST   /v1/returns/{return_id}/cancel` -> ReturnOut
- `GET    /v1/returns/settings` -> ReturnSettingsOut
- `PUT    /v1/returns/settings` -> ReturnSettingsOut

Lifecycle status strings (in `status` and in each transition):

- `drafted` opened from an approved slow-stock pickup option, waiting on the owner
- `approved` owner approved the pickup
- `collected` rep collected the goods (quantities recorded)
- `inspected` condition and disposition recorded per line, reconciled against collected
- `settled` the credit note was approved and applied against the shop's balance
- `cancelled` the owner declined the draft (terminal, nothing moved)

ReturnList: `{ "returns": [ReturnOut...], "total": int }`

ReturnOut:

```
{
  "return_id": str,
  "shop_id": str,
  "shop_name": str,
  "source": "slow_stock" | "manual",
  "approval_id": str | null,
  "reason": str,
  "status": one of the 6 strings,
  "lines": [ReturnLineOut...],
  "value_drafted_kobo": int,
  "value_collected_kobo": int,
  "value_returned_kobo": int,
  "recovered_kobo": int,
  "pickup_cost_kobo": int | null,
  "saving_kobo": int | null,
  "saving_basis": str,
  "credit_kobo": int | null,
  "credit_approval_id": str | null,
  "credit_applied_kobo": int | null,
  "credit_on_account_kobo": int | null,
  "transitions": [ {"status": str, "at": str, "by": str, "note": str} ],
  "created_at": str, "updated_at": str
}
```

ReturnLineOut. The console maps: product=`item`, quantity=`quantity_drafted`,
unit=`unit`, reason=`reason`, collected=`quantity_collected`, came back=
`quantity_returned`, disposition=`disposition`:

```
{
  "line_no": int,
  "sku": str,
  "item": str,
  "unit": str,
  "reason": str,
  "quantity_drafted": int,
  "quantity_collected": int | null,
  "quantity_returned": int | null,
  "unit_price_kobo": int,
  "value_drafted_kobo": int,
  "value_returned_kobo": int | null,
  "condition": "" | "resellable" | "damaged" | "expired",
  "disposition": "" | "resell" | "write_off" | "return_to_supplier",
  "expires_on": "YYYY-MM-DD" | null,
  "batch": str,
  "expiry_source": "invoice" | "estimated"
}
```

`expiry_source` is `invoice` when the distributor's own delivery note carried
the expiry and batch, `estimated` when it did not. A shop count is never
shown as a date.

Request bodies (every field optional unless noted):

- `POST /v1/returns` `{"approval_id": str}` OR
  `{"shop_id": str, "reason": str, "lines": [{"sku": str, "quantity": int, "reason": str}]}`
- `/approve` `{"by": str, "note": str}`
- `/collect` `{"by": str, "lines": [{"line_no": int, "quantity": int}]}` (omit lines to take what was drafted)
- `/inspect` `{"by": str, "lines": [{"line_no": int, "quantity_returned": int, "condition": str, "disposition": str}]}`
- `/settle` `{"by": str, "amount_kobo": int}` (default `value_returned_kobo`)
- `/cancel` `{"by": str, "note": str}`
- `PUT /v1/returns/settings` `{"pickup_cost_kobo": int}`

ReturnSettingsOut:

```
{ "pickup_cost_kobo": int | null, "pickup_cost_naira": str, "note": str }
```

Errors: 409 when `quantity_returned > quantity_collected`; 409 when a line is
missing at inspection; 409 when the settle `amount_kobo > value_returned_kobo`;
409 on a transition not allowed from the current status; 409 when a pickup was
not approved, its option is not a pickup, or the owner has undone the pickup;
409 when the undo finds an invoice the credit came off no longer on the book;
422 when `amount_kobo < 1`; 404 unknown return.

Settlement rules the console can rely on:

- Settling twice with the same amount hands back the waiting card (no second
  card, no second approval).
- Settling with a **different** amount supersedes the waiting card and returns
  a new `credit_approval_id` carrying the new amount.
- A card the owner rejected, withdrew or superseded is not the return's
  credit: `credit_kobo` and `credit_approval_id` read as null, and the next
  settle drafts a fresh card. A rejection never blocks settlement.
- Undoing an applied credit subtracts that credit's own applications from the
  invoices: it stays correct when two returns credit one invoice (either order
  can be undone) and when a verified bank payment or Go's morning roll moved
  the invoice first. The invoice's word (`open`, `overdue`, `settled`) is
  recomputed from the balance the undo leaves and the due day, so a credit is
  never stuck with no way to reverse it. The only refusal is an invoice that
  is no longer on the book.
- Undoing the pickup (the `slow_stock` approval) cancels a return opened from
  it that has collected nothing, and no return can be opened or walked from an
  undone pickup.

## 2. Draft credit on settlement

`kind` (approvals row `tool` = `intent_kind` = payload `kind`): `return_credit`

It rides the normal approvals route and behaves exactly like the Zoho card:

- `POST /v1/approvals/{approval_id}/approve` applies the credit to the shop's open invoices, oldest first
- `POST /v1/approvals/{approval_id}/undo` reverses it

Approval payload keys:

```
{
  "kind": "return_credit",
  "return_id": str,
  "shop_id": str,
  "shop_name": str,
  "amount_kobo": int,
  "amount_ngn": int,
  "currency": "NGN",
  "reason": str,
  "lines": [ {"item": str, "quantity": int, "unit": str, "disposition": str, "value_kobo": int} ],
  "applied_kobo": int,
  "on_account_kobo": int,
  "invoices": [ {"invoice_number": str, "amount_applied_kobo": int} ],
  "date": "YYYY-MM-DD",
  "result": null
}
```

After approve, `result` holds `{"credit_id": str, "applied_kobo": int,
"on_account_kobo": int, "undo_available": bool, "applied_at": str}`. The
return stays `inspected` until the approval is applied, then becomes
`settled`.

## 3. Exposure

Path: `GET /v1/credit/exposure`

Ageing buckets, defined once in the API (`mimi/credit/exposure.py`) from the
same `days_overdue` `mimi/owed.py` already defines (`today - due_on`, clamped
at 0; overdue is `outstanding > 0` and `days_overdue > 0`):

| key | days_overdue | weight_pct |
|---|---|---|
| `current` | 0 | 100 |
| `days_1_30` | 1..30 | 125 |
| `days_31_60` | 31..60 | 150 |
| `days_over_60` | 61 or more | 200 |

`exposure_kobo` = sum over open receivables of `round(outstanding_kobo *
weight_pct / 100)`, summed in code. The weights are an assumption, not money
anyone owes; `outstanding_kobo` and `overdue_kobo` are the money owed, and
the weights are always returned beside the figure.

Response:

```
{
  "as_of": "YYYY-MM-DD",
  "policy_version": "exposure-v0.1",
  "note": str,
  "buckets": [
    {"key": "current", "label": "Not yet due", "from_days": null, "to_days": 0, "weight_pct": 100},
    {"key": "days_1_30", "label": "1 to 30 days", "from_days": 1, "to_days": 30, "weight_pct": 125},
    {"key": "days_31_60", "label": "31 to 60 days", "from_days": 31, "to_days": 60, "weight_pct": 150},
    {"key": "days_over_60", "label": "Over 60 days", "from_days": 61, "to_days": null, "weight_pct": 200}
  ],
  "shops": [ExposureRow],
  "total": ExposureRow
}
```

ExposureRow:

```
{
  "shop_id": str, "shop_name": str,
  "outstanding_kobo": int, "outstanding_naira": str,
  "overdue_kobo": int, "overdue_naira": str,
  "oldest_days_overdue": int,
  "buckets": {"current_kobo": int, "days_1_30_kobo": int, "days_31_60_kobo": int, "days_over_60_kobo": int},
  "exposure_kobo": int, "exposure_naira": str,
  "verified_paid_kobo": int,
  "shown_not_counted_kobo": int,
  "open_invoices": int,
  "confidence": "low" | "medium" | "high",
  "note": str
}
```

`total` is `shop_id: ""`, `shop_name: "All shops"`.

Confidence is defined, not felt. It measures how much the bank feed backs the
figure, from the count of bank-verified payments in the last 8 weeks: 8 or
more is `high`, 4 to 7 is `medium`, fewer than 4 is `low`. The total's
confidence is the weakest shop's with exposure.

Only bank-verified payments count: receivables are reduced only by the bank
feed, so a shop-reported payment never lowers exposure; it appears as
`shown_not_counted_kobo`.

## Tests

`tests/test_returns.py` (26 tests) and `tests/test_exposure.py` (9 tests)
cover every string above, including the unhappy paths and the two
person-read-string guarantees: tenant filtering, and no internal id anywhere
a person reads.
