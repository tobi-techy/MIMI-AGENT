# The owner's rulebook (RAI-323)

Each distributor decides, per action type, whether Mimi acts on her own, acts
only when something is pre-approved, asks first, or hands the step to a
person. Mimi proposes, code checks, the owner approves: the rulebook only
moves steps between "Mimi does it" and "the owner decides". It can never
let Mimi move money.

## The seam

```python
from mimi.harness import policy
d = policy.decide(tenant, action_type, {"tool": tool, "payload": payload, "preapproved": False})
d.behaviour   # act | act_if_preapproved | ask | hand_off | never (built-in stop)
d.tier        # auto | notify | approve | never: what the loop and Go do
d.outcome     # act | ask | hand_off | refuse
d.source      # built_in | default | owner | fail_closed | unknown
```

`GoPolicy.decide` is the implementation; `GoPolicy.tier(tenant, tool,
payload)` is a thin call to it, so every existing caller already goes
through the rulebook. In the loop, `Harness._propose` calls it for every
write intent and emits a `policy_decided` event (the audit row in the
thread's event log, and a span in the trace).

Order of checks:

1. **Built-in stops** (below): `never`, locked. No rule, API call, chat or
   shop message changes them. A tool from a stop is a stop whatever action
   type it is named under.
2. **The tool's policy row** (`BASE_TIERS`). No row: `never` (fail closed).
3. **The owner's rule**, or the default. Rows below the action's floor are
   ignored. An action type with no catalogue entry is `ask`, never `act`.
   If the rule store cannot be read, every owner-settable step asks.
4. **Code's own rules** (the amount limit, firm tone, `ALWAYS_APPROVE`) can
   only make a step stricter: `act` means "go ahead within code's limits".

| Behaviour | Tier |
| -- | -- |
| act | code's tier (auto or notify; approve when an amount or tone rule says so) |
| act_if_preapproved | approve, unless code found a standing pre-approval: then notify |
| ask | approve (the existing approvals flow) |
| hand_off | approve, as a hand-off: the approval card says the owner does it by hand, and approving it never sends Mimi's draft |

`preapproved` is code's word only (`Harness(preapprovals=...)`, a callable
`(tenant, action_type, payload) -> bool`). Nothing is wired to it yet, so
today `act_if_preapproved` behaves as `ask`; the trust ladder (RAI-288) is
where pre-approvals will come from.

The defaults reproduce the tiers from before the rulebook exactly
(`tests/test_policy_rulebook.py` compares every tool and payload with the
old code), so `POLICY_VERSION` is unchanged and no recorded answer misses.

## Owner-settable action types

| Action type | Helper | What it is | Default | Owner may pick |
| -- | -- | -- | -- | -- |
| `collector.send_reminder` | collector | Send a payment reminder to a shop | act | act, act_if_preapproved, ask, hand_off |
| `collector.plan_reminders` | collector | Plan the day's reminders | act | act, act_if_preapproved, ask, hand_off |
| `collector.send_receipt` | collector | Send a shop a payment receipt | act | act, act_if_preapproved, ask, hand_off |
| `collector.create_payment_account` | collector | Give a shop an exact-amount account to pay into | act | act, act_if_preapproved, ask, hand_off |
| `collector.notify_rep` | collector | Tell a sales rep about a shop | act | act, act_if_preapproved, ask, hand_off |
| `shop.send_message` | all | Send a shop a message | act | act, act_if_preapproved, ask, hand_off |
| `shop.ask_question` | all | Ask a shop a question | act | act, act_if_preapproved, ask, hand_off |
| `rep.send_message` | all | Send a sales rep a message | act | act, act_if_preapproved, ask, hand_off |
| `order.confirm_reorder` | order | Confirm a shop's order | act_if_preapproved | act_if_preapproved, ask, hand_off |
| `order.ask_question` | order | Ask a shop about its order | act | act, act_if_preapproved, ask, hand_off |
| `order.send_delivery_receipt` | order | Send a shop a delivery receipt | act | act, act_if_preapproved, ask, hand_off |
| `order.record_delivery_consent` | order | Record a shop's delivery answer | act | act, act_if_preapproved, ask, hand_off |
| `order.ask_receipt_again` | order | Ask a shop to answer a receipt again | act | act, act_if_preapproved, ask, hand_off |
| `order.flag_delivery` | order | Flag a delivery problem to you | act | act, act_if_preapproved, ask, hand_off |
| `stock.ask_count` | stock | Ask a shop to count its stock | act | act, act_if_preapproved, ask, hand_off |
| `stock.record_count` | stock | Record a shop's stock count | act | act, act_if_preapproved, ask, hand_off |
| `stock.draft_restock` | stock | Draft a restock order | act_if_preapproved | act_if_preapproved, ask, hand_off |
| `credit.ask_shelf_photo` | credit | Ask a shop for a shelf photo | act | act, act_if_preapproved, ask, hand_off |
| `credit.suggest_limit` | credit | Suggest a credit limit for you to decide | ask | ask, hand_off |
| `proof.record_payment` | proof | Record a checked payment proof | act | act, act_if_preapproved, ask, hand_off |
| `proof.flag_payment` | proof | Flag a payment proof that looks wrong | ask | ask, hand_off |
| `brand.send_email` | brand | Email a brand or bank | ask | ask, hand_off |
| `plan.raise_allowance` | mimi | Raise today's model allowance | ask | ask, hand_off |

Steps inside Mimi's own working (her plan, timers, handing work between
helpers, telling or asking the owner, reads, skills) also pass through
`decide` as `internal.*` and are always `act`; they are not in the owner's
list.

## Built-in stops

| Built-in stop | What it is | Tools |
| -- | -- | -- |
| `payment.move_money` | Move money (pay, send, refund) | `transfer_money`, `send_money`, `pay_out`, `refund_payment` |
| `bank.change_details` | Change bank details | `change_bank_details`, `update_bank_account` |
| `credit.set_limit` | Set or change a credit limit | `set_credit_limit`, `change_credit_limit`, `raise_credit_limit`, `approve_credit_request` |
| `debt.change_terms` | Write off debt, change an amount, extend a due date or give a discount | `write_off_debt`, `change_invoice_amount`, `extend_due_date`, `give_discount` |
| `account.ask_pin_or_otp` | Ask anyone for a PIN or OTP | `request_pin_or_otp` |
| `account.change_password_or_key` | Change a password or key | `change_password`, `reset_password`, `rotate_api_key`, `change_api_key` |
| `shop.delete_data` | Delete a shop's data | `delete_shop`, `delete_shop_data`, `erase_shop_data` |

## API (owner key only)

* `GET /v1/policy`: every owner-settable step with its behaviour, default,
  the behaviours allowed and who changed it last, then the built-in stops
  with `locked: true`, plus the last 20 changes. `editable` is false in
  demo mode and with `MIMI_POLICY_RULES=0`.
* `PUT /v1/policy` with `{"rules": [{"action_type": "...", "behaviour": "..."}], "by": "..."}`:
  all items are checked first (409 for a built-in stop, an internal step,
  an unknown action type or a behaviour below the floor; 422 for `never`),
  so one bad item changes nothing. Each change is a `policy_changes` row
  with who made it and the key's digest. It takes effect on the next turn
  (the rules are read fresh on every decision). A channel key gets 403; a
  demo session or demo mode gets 403.

Nothing a helper can call writes a rule. "You can send it now" in chat, or
any shop message, changes nothing; only the API does.

## Store

Alembic `0010`: `policy_rules` (one row per action type the owner changed),
`policy_changes` (the log) and `approvals.handling` (`""` or `hand_off`).

## Rollout and rollback

`MIMI_POLICY_RULES=0` ignores every owner row (the defaults only) and makes
`PUT` answer 409. With no rows the defaults are today's behaviour.

## Eval

`python -m mimi.harness.policy_eval` (CI): every owner-settable action type
times every behaviour, every built-in stop times every behaviour (refused by
the API check, and `never` even when a row is written straight into the
store), and scripted loosening attempts by shop and chat messages. Pass
marks: 100% of cases, zero built-in stop bypasses.
