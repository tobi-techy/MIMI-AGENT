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

**With the switch OFF, `GET /v1/inbox` still works.** The route exists and
returns items; it does not error and it does not pretend. Every applied
`ask_helper` intent still records one inbox item in state `queued` (a record,
not execution: no wake, no turn, no message). Those items stay `queued`
because nothing runs them. When the switch is ON a `queued` item moves to
`working` then `done` (or `failed`/`handed_back`). So a console reading with
the switch off sees the assignments that exist, honestly marked as not started,
not an empty list and not an error.

## 1. Inbox item (`InboxItem`)

`from_helper: "mimi"` means the **owner's own ask**, routed by Mimi: the owner
asked for this work and Mimi passed it to a helper. The console must render it
as the owner's own assignment ("You asked Collector to ..."), never as a task
"from Mimi" to a helper. Every other `from_helper` is a real helper id.

Fields are machine values or sentences; the console renders the sentence, never
a code. A person must never read a code (the console's hard rule).

| Field | Kind | The console renders it |
|---|---|---|
| `task_id`, `contract`, `state`, `thread_id`, `turn_id`, `chain`, `parent_task_id`, `created_at`, `finished_at`, `shop_id` | machine | no |
| `from_helper`, `to_helper`, `skill` | machine id | via `from_title`/`to_title` |
| `from_title`, `to_title` | name | yes |
| `shop_name` | name | yes |
| `ask` | sentence (the sender's words) | yes |
| `reason` | sentence (a plain ack like "Asking Proof", or why it was refused) | yes, as the line above the status |
| `outcome` | sentence | yes: the one sidebar/status sentence |
| `result` | sentence | yes, when `state` is `done` |
| `state` | closed set | the console maps it to a face, never prints it |

`reason` and `outcome` are always plain words written by code; neither is ever
a code, an id or a JSON blob. `outcome` is never empty; `result` is `""` until
the item is `done`.

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
| `reason` | string | **a sentence, never a code**; the console renders it as written (see below) |
| `outcome` | string | one short present-tense sentence for the sidebar: the live action while open, what came back when done; `""` is never sent |
| `result` | string | what came back, written by code from the receiver's own reply; `""` until `done` |
| `chain` | list | machine data only; **not for display** (see below) |
| `parent_task_id` | string | the item that asked for this one, or `""` |
| `thread_id` | string | the receiver's own thread |
| `turn_id` | string | the child turn, or `""` until it is created |
| `created_at` | string | ISO 8601 with zone |
| `finished_at` | string | ISO 8601 with zone, or `""` |

`chain` root-first is the job tree: `["collector", "proof"]` means Collector
handed to Proof. The harness refuses a `ask_helper`/`delegate_task` to a
helper already in the chain (a loop) or past depth 2.

## 1a. Which fields are sentences and which are codes

The console must never print a code. Only these fields are machine values and
are never rendered: `task_id`, `contract`, `state`, `thread_id`, `turn_id`,
`chain`, `parent_task_id`, `from_helper`, `to_helper`, `skill`, `shop_id`,
`created_at`, `finished_at`. Each is paired with a rendered field
(`from_helper`→`from_title`, `to_helper`→`to_title`, `shop_id`→`shop_name`) or
is not shown at all.

Everything else a person reads is a **sentence written by code**, not by a
model in this module:

* **`reason` is a sentence, never a code.** It is always plain English written
  by code: the ack (`"Asking Proof"`, `"Collector handed it back; asking
  Proof"`) or, on a `refused` item, why it was refused (`"Proof is already
  working on this; handing it back would loop."`). It is never a bare token,
  never a state word, never an id and never JSON. The console renders
  `reason` as-is. If it is ever empty (an item with no receiver), the console
  falls back to `outcome`, which is never empty; it must **not** fall back to
  `state`, because `state` is the code.
* `outcome` is a sentence and is never empty; render it as the one status line.
* `result` is a sentence, set only when `state` is `done`.
* `ask` is the sender's own sentence (model-written, figure-checked).

So: render `reason` (or, when empty, `outcome`). Never render `state` as words
of your own for a failed or refused item; the sentence is already there.

`chain` is **machine data, not for display.** Its shape may change between
contract values, so the console must not print it, map it to text, or derive a
board from it. It exists for the depth and loop guards. This is deliberate,
not an omission.

## 1b. One item renders exactly once

A handoff is one item and becomes **exactly two lines**, no more: one in the
**receiving** helper's conversation ("Task from Collector", then the receiver's
own words as they arrive) and one in the **sending** helper's conversation (the
result when it lands). Nothing else renders it.

The inbox is the single rendering of a handoff. When `GET /v1/inbox` is live,
the console **drops** its thread-derived task sections in favour of the inbox;
nobody re-adds a second rendering (no card on a board, no duplicate list row,
no third line on the owner thread). An item that arrived from the owner's own
ask (`from_helper: "mimi"`) renders as the owner's own assignment, in the
receiving helper's conversation only.

## 1c. The example item

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
| `refused` | a guard refused the handoff (its own chain, depth past 2, self, no skill); `reason` says why, in words |

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
  "by_state": {"done": 1},
  "helpers": [ "..." ]
}
```

`by_state` counts every item that matches `helper` (before `state`/`limit`
narrow it), so the console can show a per-helper badge without a second call.

The same response carries the team line the sidebar needs, one honest sentence
per helper, derived in code from inbox items and open turns (never a stored
state word; a helper with no item and no open turn is absent). `outcome` is the
newest item's sentence, open or finished, so the sidebar shows what a helper
did last as well as what she is doing now:

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

`GET /v1/threads/{thread_id}`, when the thread is a helper's own thread
(`{org}:helper:{helper}`, `ThreadDetail.inbox`), returns the same items newest
first: the handoffs she received **and** the ones she sent. So her conversation
is her inbox, and the console needs no `/v1/inbox` call to draw it. On any
other thread (`ThreadDetail.inbox` too, via the contract field of the same
name) the thread's own handoffs only; `inbox` is `[]` on a thread with none.

## How the console renders it (do not build a screen)

This endpoint is data, not a screen. The console renders an item as a dated
line inside the **receiving** helper's conversation ("Task from Collector",
then the receiver's own words as they arrive), and the same item's result as a
line inside the **sender's** conversation. Two lines per item, exactly (see
1b). The inbox record is the data; the conversation is the inbox. No board, no
drag and drop, no assignment control, no column of cards: those make the owner
a dispatcher. `chain` is never displayed (1a). Steps, when surfaced, are a
count ("3 of 5 done"), never a percentage. v1 carries no steps.

When this route goes live, the console drops its thread-derived task sections:
the inbox is now the single rendering of a handoff, and a second one must not
be re-added.

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
