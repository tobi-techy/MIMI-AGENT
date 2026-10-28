---
name: payment-check
description: Someone says a payment was made, in words without a picture: I have paid, I sent the transfer, I gave the rep cash; or the owner asks whether a shop's payment landed.
helper: collector
version: 1.0.0
status: active
tier: notify
consent: [shop:balance, shop:invoices, shop:payments, shop:messages, org:receivables, org:helpers, org:owner, org:reps, org:plan]
tools: [check_payment, get_balance, get_invoices, get_payment_history, send_receipt, ask_helper, notify_rep, set_followup, send_to_user, ask_question, update_plan]
scripts: []
senders: [shop, rep, owner]
money: true
---

A shop says it paid: check_payment. Matched: send_receipt and thank them. Not matched: tell the shop it is pending, never "received". A screenshot goes to Proof with ask_helper.
The owner asks whether a shop's payment landed: check_payment, then send_to_user to owner_console with what the result shows.
If a payment matches two shops or invoices: ask_question to the owner, do not guess.
Recheck a pending payment later with set_followup (in_hours from the result's next_step, never a guess).
