# Helper inboxes: the console contract

Every helper has its own inbox. When one helper hands work to another, that
work appears in the **receiving** helper's inbox as one item, and the item
carries what came back. This page is the contract the console reads for it.

It is stable: field names do not change without a new `contract` value
(`mimi.api.inbox.CONTRACT_VERSION`). New fields may be added; none is removed
or renamed inside one contract value.

Code: `mimi/api/inbox.py` (the records), `mimi/api/reads.py` (`list_inbox`),
`mimi/store/schema.py` (`helper_tasks`), `mimi/api/delegation.py` (the
transport that wakes the receiver). OpenAPI: `docs/api/openapi.json`
(`InboxItem`, `InboxList`).

## Switch

Creating an item and waking the receiver both sit behind
`MIMI_HELPER_DELEGATION`, default **off** in the repo and on in the
deployment. Off, a helper's toolset and recorded requests are byte-identical
to before: `ask_helper` (Collector only, targets `proof`, `credit`, `order`)
still records an intent and nothing else. On, all six helpers also gain
`delegate_task` (any of the six targets) and an applied call wakes the
receiver. See `mimi/api/helpers.py` (`_with_delegation`), the same pattern as
`MIMI_COMPUTER_TOOLS`.

## 1. Inbox item (`InboxItem`)

| Field | Type | What |
|---|---|---|
| `task_id` | string | `itk_<task_id>`; one item, however many times the intent is applied |
| `contract` | string | `helper-inbox-2026-11-04.1` |
| `from_helper` | string | who sent the work: `mimi` for an owner assignment, else a helper id |
| `from_title` | string | display name: `Collector` |
| `to_helper` | string | the receiving helper: `order`, `collector`, `credit`, `stock`, `brand`, `proof` |
| `to_title` | string | display name: `Proof` |
| `skill` | string | the `helper/skill` the receiver runs; `""` when none was chosen |
| `shop_id` | string | the shop the work concerns, or `""` |
| `shop_name` | string | the shop's book name, or `""` |
| `ask` | string | what was asked, in the sender's own words (model-written, figure-checked; never shop text) |
| `state` | string | `queued`, `working`, `done`, `handed_back`, `failed` or `refused` |
| `reason` | string | one line written by code: the ack, or why it was refused |
| `outcome` | string | one short present-tense sentence for the sidebar: the live action while open, what came back when done; `""` is never sent |
| `result` | string | what came back, written by code from the receiver's own reply; `""` until `done` |
| `chain` | list | the helpers above and including the receiver, root first |
| `parent_task_id` | string | the item that asked for this one, or `""` |
| `thread_id` | string | the receiver's own thread |
| `turn_id` | string | the child turn, or `""` until it is created |
| `created_at` | string | ISO 8601 with zone |
| `finished_at` | string | ISO 8601 with zone, or `""` |

`chain` root-first is the job tree: `["collector", "proof"]` means Collector
handed to Proof. The harness refuses a `ask_helper`/`delegate_task` to a
helper already in the chain (a loop) or past depth 2.

```json
{
  "task_id": "itk_a1b2c3d4e5f6",
  "contract": "helper-inbox-2026-11-04.1",
  "from_helper": "collector",
  "from_title": "Collector",
  "to_helper": "proof",
  "to_title": "Proof",
  "skill": "proof/fake-alert-check",
  "shop_id": "shop-001",
  "shop_name": "Mama Sola Provisions",
  "ask": "Check Mama Sola's payment screenshot against the bank feed",
  "state": "done",
  "reason": "Asking Proof",
  "outcome": "Checked Mama Sola's screenshot: no bank credit matches, likely fake",
  "result": "No bank credit matches; the alert is likely a fake.",
  "chain": ["collector", "proof"],
  "parent_task_id": "",
  "thread_id": "coral-lane:helper:proof",
  "turn_id": "turn_6a1f0c2e",
  "created_at": "2026-11-04T14:32:00+00:00",
  "finished_at": "2026-11-04T14:32:02+00:00"
}
```

## 2. The states

| State | Means |
|---|---|
| `queued` | recorded; the receiver has not started |
| `working` | the receiver's turn is running |
| `done` | the receiver finished; `result` says what came back |
| `handed_back` | the receiver could not do it and returned it to the sender |
| `failed` | the receiver's run failed |
| `refused` | a guard refused the handoff (its own chain, depth past 2, self, no skill) |

A shadowed assignment (`mimi.harness.go` shadow mode) writes **no** item: it
is not delivered work, so it never appears in an inbox.

## 3. Endpoint

`GET /v1/inbox` (owner key). Read-only.

| Query | Type | Default | What |
|---|---|---|---|
| `helper` | string | `""` (all) | one of the six helper ids |
| `state` | string | `""` (all) | one of the six states |
| `limit` | int 1..200 | 50 | newest first |

```json
{
  "items": [ { "task_id": "itk_...", "contract": "helper-inbox-2026-11-04.1", "...": "..." } ],
  "total": 1,
  "by_state": {"done": 1}
}
```

`by_state` counts every item that matches `helper` (before `state`/`limit`
narrow it), so the console can show a per-helper badge without a second call.

The same response carries the team line the sidebar needs, one honest sentence
per helper, derived in code from open turns and open inbox items (never a
stored state word; a helper with nothing open is absent):

```json
{
  "helpers": [
    {"helper": "proof", "helper_name": "Proof", "open": 1, "waiting_on_owner": 0,
     "outcome": "Checking Mama Sola's screenshot"},
    {"helper": "collector", "helper_name": "Collector", "open": 0, "waiting_on_owner": 0,
     "outcome": "Handed work to Proof"}
  ]
}
```

`open` is items not yet `done`/`failed`/`refused` plus turns still queued or
running; `waiting_on_owner` is a paused turn or a pending approval, so the
console derives the face state (`working`, `waiting`, `blocked`, `done`,
`idle`) from these counts instead of a stored word.

`GET /v1/threads/{thread_id}` for a `{org}:helper:{helper}` thread returns the
same items under `inbox` when the thread is a helper's own thread.

## How the console renders it (do not build a screen)

This endpoint is data, not a screen. The console renders an item as a dated
line inside the **receiving** helper's conversation ("Task from Collector",
then the receiver's own words as they arrive), and the same item's result as a
line inside the **sender's** conversation. The inbox record is the data; the
conversation is the inbox. No board, no drag and drop, no assignment control,
no column of cards: those make the owner a dispatcher. Steps, when surfaced,
are a count ("3 of 5 done"), never a percentage. v1 carries no steps.

## 4. Deliberately absent (v1)

* No approval, no money, no amounts. A delegation is internal work; it never
  moves money, never writes a shop message and never raises an approval.
* No shop text. `ask` is the sender's own words, figure-checked like every
  other model-written field (`loop.FIGURE_CHECKED`); shop words are never
  copied into an instruction for another helper.
* No per-helper auth. v1 is owner-key, read-only; helpers do not authenticate.
* No push channel. Poll `GET /v1/inbox`.
* No edit, delete or acknowledge, and no separate `unread` counter.
* No tool a helper may call to *read* its own inbox in v1: the inbox is a
  console surface first.
* No board and no step list. No stored face state: the console derives
  `working`/`waiting`/`blocked`/`done`/`idle` from `open` and
  `waiting_on_owner`, and no percentage is ever sent.
