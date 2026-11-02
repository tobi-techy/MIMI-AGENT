# Helper handoffs: the console contract

Mimi is the Chief of Staff. Every message lands on her; she hands it to one
helper (Order, Collector, Credit, Stock, Brand, Proof) and the helper answers.
This page is the contract the console reads for that, on every turn. It is
stable: field names do not change without a new `contract` value
(`handoffs-2026-10-30.1`, `mimi.harness.handoff.CONTRACT_VERSION`). New
fields may be added; none is removed or renamed inside one contract value.

Code: `mimi/harness/handoff.py` (the typed records, the hand-back rule,
chips, deflection words), `mimi/api/cards.py` (cards read from the book),
`mimi/api/agent.py` (`_hand_back`, `_handoff_records`), `mimi/api/reads.py`
(`list_tasks`). OpenAPI: `docs/api/openapi.json` (`TurnOut`, `Handoff`,
`Handback`, `Card`, `Chip`, `TaskList`).

Where it shows up:

| Where | What |
|---|---|
| `POST /v1/turns`, `GET /v1/turns/{turn_id}` | `handoff`, `handback`, `card`, `chips`, `task_status` on `TurnOut` |
| `GET /v1/threads/{thread_id}` | `handoffs` (one per turn, oldest first); `steps` gains `handoff`, `card_read` and `handoff_result` events |
| `GET /v1/tasks` (owner) | each helper task with its status: `working`, `needs_you`, `done`, `handed_back`, `failed` |

The rules behind it:

* Every routed turn carries a `handoff`, and the thread gets a `handoff`
  event ("Asking Collector") timed before the helper's first reply, then a
  `handoff_result` event after it (Mimi posting the result back on the
  thread). While the turn is queued or running, `GET /v1/turns/{id}` already
  answers `handoff.status: "working"`.
* Never a dead end. A helper that points the person somewhere else instead
  of answering ("the orders helper or your rep will help you with that") is
  held by the claims gate (`mimi.harness.claims.deflections`, problem kind
  `deflection`): it gets the run's one repair. If it still cannot answer, the
  message goes back to Mimi as a typed `handback` and she re-routes it once
  (max one hop). After that hop Mimi answers herself from the book (when the
  skill has a card) or asks one clear question. Never a third helper.
* Cards are for the owner console only, and every figure on one is read and
  summed from the book by code. A shop never sees another shop's row.
* With a card, the reply is one sentence. For a pure read (no write
  proposed) the sentence is written by code from the same rows as the card;
  when the helper did work (drafted reminders, say) its own first sentence is
  kept, after the claims gate passed it.
* Chips are safe tier only: a chip asks Mimi something (a new turn, routed
  and guarded like any other) or opens a page. No chip or card action
  approves, sends money or changes a record. Money moves stay approval-only.
* The owner may assign a helper: `@Collector ...` at the start of a message
  (`@Orders` works too), or `assign_to` on `POST /v1/turns`. It overrides
  routing to that helper's owner skills only, under every policy tier. A
  shop's @mention is never honoured; `assign_to` from anyone but the owner is
  422.

## Routing fixes behind it

* "Any orders available now", "any new orders", "show me the orders" from the
  owner go to `order/open-orders` by rule (`router.OWNER_RULES`, never the
  model), answered by code with a list card of the orders waiting in
  approvals. "What's in stock", "stock levels" go to `stock/low-stock-report`,
  answered by code with the items the stock estimator says may run out within
  3 days. Neither ever reaches Brand.
* Shops: "is X available", "what's in stock", "can I order X", "what goods do
  you have", "any noodles available" are `order/order-intake` phrase rules
  (`lexicon.py`); "any promo available" stays Brand.
* `order/open-orders` is a rule-only skill: the routing model's catalogue
  text is unchanged, so `CATALOGUE_VERSION` and every recorded router answer
  stay valid. Its skill folder (`skills/open-orders`) is a draft until a model
  body is written for it.

Eval: `python -m mimi.harness.router_eval` runs the 300 labelled messages
plus `eval/router/handoffs.jsonl` (owner order and stock questions, shop
availability, assignments), which must all route by rule or phrase, never
to Brand and never through the model.

## 1. Handoff (`handoff`)

| Field | Type | What |
|---|---|---|
| `handoff_id` | string | `ho_<turn_id>` |
| `contract` | string | `handoffs-2026-10-30.1` |
| `from_helper` | `"mimi"` | always Mimi |
| `to_helper` | string | `order`, `collector`, `credit`, `stock`, `brand`, `proof`, `mimi` |
| `to_title` | string | display name: `Collector` |
| `skill` | string | `helper/skill` |
| `reason` | string | the one-line ack, written by code: `Asking Collector`; after a hand-back `Brand handed it back; asking Order`; `Mimi is answering` |
| `via` | string | how the helper was picked: `rule`, `lexical`, `fast`, `model`, `mention`, `assign`, `handback`, `binding` |
| `hop` | int | 0, or 1 after a hand-back |
| `status` | string | `working`, `needs_you`, `done`, `handed_back`, `failed` |
| `steps` | list | `{name, kind, status, ms, at}`; `kind` is `route`, `tool`, `model`, `reply`, `handback` or `code`; `status` is `ok`, `refused`, `held` (claims gate) or `failed`; `ms` is the step's own time |
| `started_at`, `finished_at` | string | ISO 8601 with zone |

```json
{
  "handoff": {
    "handoff_id": "ho_turn_6a1f0c2e9b7d4e51a3c8",
    "contract": "handoffs-2026-10-30.1",
    "from_helper": "mimi",
    "to_helper": "collector",
    "to_title": "Collector",
    "skill": "collector/chase",
    "reason": "Asking Collector",
    "via": "lexical",
    "hop": 0,
    "status": "done",
    "steps": [
      {"name": "route:lexical", "kind": "route", "status": "ok", "ms": 0.41, "at": "2026-10-30T14:32:00+00:00"},
      {"name": "model", "kind": "model", "status": "ok", "ms": 812.0, "at": "2026-10-30T14:32:00.400000+00:00"},
      {"name": "get_invoices", "kind": "tool", "status": "ok", "ms": 6.2, "at": "2026-10-30T14:32:01.220000+00:00"},
      {"name": "send_to_user", "kind": "reply", "status": "ok", "ms": 0.0, "at": "2026-10-30T14:32:02.010000+00:00"},
      {"name": "card:shop_row", "kind": "code", "status": "ok", "ms": 0.0, "at": "2026-10-30T14:32:02.050000+00:00"}
    ],
    "started_at": "2026-10-30T14:32:00+00:00",
    "finished_at": "2026-10-30T14:32:02.050000+00:00"
  }
}
```

## 2. Hand-back (`handback`)

Present when a helper returned the message to Mimi. `null` otherwise.

| Field | Type | What |
|---|---|---|
| `from_helper`, `from_skill` | string | the helper that handed it back |
| `code` | string | `deflected` (it pointed elsewhere and said nothing useful), `failed` (it kept deflecting after its repair), `no_helper` (no helper for that skill yet), `no_tools` |
| `reason` | string | one line, code-written |
| `to_helper`, `to_skill` | string | where Mimi sent it; `mimi` when she answered herself; empty when she asked |
| `resolved` | string | `rerouted`, `mimi_answered`, `asked_owner`, `asked_sender` |
| `spans` | list | the deflecting words the gate caught (at most 4) |
| `first_run_id` | string | the first helper's run id (its trace) |

```json
{
  "handback": {
    "from_helper": "brand",
    "from_skill": "brand/promo-info",
    "code": "failed",
    "reason": "Brand could not answer without pointing elsewhere",
    "to_helper": "order",
    "to_skill": "order/open-orders",
    "resolved": "rerouted",
    "spans": ["the orders helper or your rep will help"],
    "first_run_id": "run_2f6b0e9a41c3"
  },
  "task_status": "done"
}
```

## 3. Answer card (`card`)

`kind` is `shop_row` or `list`. `source` is always `book`. Money is integer
kobo with a naira string beside it, both from code.

| Field | Type | What |
|---|---|---|
| `kind`, `title`, `skill` | string | |
| `shop_row` | object or null | `shop_id`, `name`, `owed_kobo`, `owed_naira`, `overdue_kobo`, `overdue_naira`, `days_late` (oldest overdue invoice), `credit_limit_kobo`, `credit_limit_naira`, `open_invoices` |
| `items` | list | `item_id`, `label`, `qty` (int or null), `unit`, `status`, `amount_kobo` (int or null), `amount_naira`, `shop_id` |
| `total` | int | rows in the book behind the card (a list shows at most 8) |
| `total_kobo`, `total_naira` | int or null, string | the sum of every row's amount behind a list (all rows, not only those shown), summed in code; null when the rows carry no amount |
| `empty_text` | string | shown when `items` is empty |
| `actions` | list | `{action, label, intent, tier}`; `action` is `remind`, `open_shop`, `open_order`, `open_approvals` or `ask`; `intent` as for chips |
| `as_of` | string | the book's day |

What code read for a card is kept on the turn as a `card_read` thread event
(its `data` is the card), like a tool read, so every figure in the card's
sentence can be traced to this turn; the e2e eval's invented-figure check
reads it.

Cards today: `collector/chase` (a shop row for "highest debtor"; a list of
debtors otherwise), `collector/collections` and `collector/payment-check` (a
shop row when the owner names one shop), `order/open-orders` (list),
`stock/low-stock-report` (list).

```json
{
  "card": {
    "kind": "shop_row",
    "title": "Highest debtor",
    "skill": "collector/chase",
    "shop_row": {
      "shop_id": "shop-001",
      "name": "Mama Sola Provisions",
      "owed_kobo": 349730000,
      "owed_naira": "N3,497,300",
      "overdue_kobo": 238725000,
      "overdue_naira": "N2,387,250",
      "days_late": 45,
      "credit_limit_kobo": 170000000,
      "credit_limit_naira": "N1,700,000",
      "open_invoices": 14
    },
    "items": [],
    "total": 1,
    "empty_text": "",
    "actions": [
      {"action": "remind", "label": "Remind Mama Sola Provisions", "tier": "safe",
       "intent": {"kind": "turn", "text": "Send shop-001 a soft reminder about the overdue balance", "assign_to": "collector", "target": ""}},
      {"action": "open_shop", "label": "Open shop", "tier": "safe",
       "intent": {"kind": "open", "text": "", "assign_to": "", "target": "shop:shop-001"}}
    ],
    "as_of": "2026-10-28",
    "source": "book"
  },
  "replies": [{"channel": "owner_console", "text": "Mama Sola Provisions owes the most: N3,497,300, 45 days late."}]
}
```

A list card (orders):

```json
{
  "card": {
    "kind": "list",
    "title": "Open orders",
    "skill": "order/open-orders",
    "shop_row": null,
    "items": [
      {"item_id": "ord-shop-002-1", "label": "Funke & Sons Mini Mart: 2 lines", "qty": 7, "unit": "units",
       "status": "waiting for you", "amount_kobo": 1550000, "amount_naira": "N15,500", "shop_id": "shop-002"}
    ],
    "total": 1,
    "total_kobo": 1550000,
    "total_naira": "N15,500",
    "empty_text": "",
    "actions": [
      {"action": "open_approvals", "label": "Open approvals", "tier": "safe",
       "intent": {"kind": "open", "text": "", "assign_to": "", "target": "approvals"}}
    ],
    "as_of": "2026-10-28",
    "source": "book"
  }
}
```

## 4. Next-step chips (`chips`)

Two or three per owner answer (none for shops). `tier` is always `safe`.
`intent.kind` is `turn` (send `POST /v1/turns` with `text`, and `assign_to`
when it is set) or `open` (navigate to `target`: `shop:<shop_id>`,
`order:<order_id>` or `approvals`; nothing is sent).

```json
{
  "chips": [
    {"label": "Remind Mama Sola Provisions", "tier": "safe",
     "intent": {"kind": "turn", "text": "Send shop-001 a soft reminder about the overdue balance", "assign_to": "collector", "target": ""}},
    {"label": "Open Mama Sola Provisions", "tier": "safe",
     "intent": {"kind": "open", "text": "", "assign_to": "", "target": "shop:shop-001"}},
    {"label": "Who else owes?", "tier": "safe",
     "intent": {"kind": "turn", "text": "Who else owes me money?", "assign_to": "collector", "target": ""}}
  ]
}
```

The reminder chip asks Collector to draft a reminder; a reminder goes
through Collector's own tiers (a firm one waits in approvals).

## 5. The owner assigns a helper (`assign_to`) and task status (`GET /v1/tasks`)

`assign_to` on `POST /v1/turns`: one of `order`, `collector`, `credit`,
`stock`, `brand`, `proof`, `mimi`. The same as starting the message with
`@Collector`. The route's `rule` is `owner_assign:@collector` (or
`owner_mention:@collector`) and `handoff.via` is `assign` (or `mention`).

```json
{
  "message_id": "console.8f2c",
  "source": "owner_message",
  "text": "any orders now?",
  "assign_to": "order"
}
```

`GET /v1/tasks` (owner key): `thread_id`, `helper`, `status`, `limit`
(1 to 200, default 50). Newest first. `by_status` counts every task that
matches `thread_id` and `helper`. A task whose approval still waits is
`needs_you`; once the owner decides it, `done`.

```json
{
  "tasks": [
    {
      "task_id": "ho_turn_cfef2b32b0be6683ce7c",
      "turn_id": "turn_cfef2b32b0be6683ce7c",
      "thread_id": "coral-lane:owner",
      "helper": "order",
      "helper_name": "Order",
      "skill": "order/open-orders",
      "status": "done",
      "reason": "Asking Order",
      "handoff": null,
      "handback": null,
      "card": null,
      "chips": [],
      "created_at": "2026-10-30T14:32:00+00:00",
      "finished_at": "2026-10-30T14:32:00.300000+00:00"
    }
  ],
  "total": 1,
  "by_status": {"done": 1}
}
```

(`handoff` in a real task is the full record from section 1; it is `null`
above only to keep the example short.)

## What is not changed

* Helper prompts and tool lists are unchanged, so every recorded model answer
  still replays. Two helper prompts still tell the model to send off-topic
  questions to "the right helper or the rep" (`mimi/brand/tools.py`
  `BRAND_SKILL`, `mimi/receipts/tools.py` `ORDER_SKILL`); the gate and the
  hand-back now catch that in code. Rewriting those lines needs a paid
  re-record of the Brand and Order evals.
* The router's model prompt, catalogue text and thresholds are unchanged.
