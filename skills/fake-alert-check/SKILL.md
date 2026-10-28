---
name: fake-alert-check
description: A payment screenshot, a debit alert or a pasted bank SMS that needs checking before anyone counts it as paid.
helper: proof
version: 1.0.0
status: active
tier: approve
consent: [shop:payments, shop:invoices, shop:messages, org:owner, org:plan]
tools: [check_payment_screenshot, record_payment_proof, flag_payment_proof, check_payment, get_payment_history, notify_owner, parse_bank_alert, send_to_user, ask_question, update_plan]
scripts: [parse_bank_alert]
senders: [shop, rep, owner]
money: true
---

A shop sent a payment screenshot (its message names a photo_id, ph_...): call check_payment_screenshot with the shop_id and photo_id, then do exactly its next_step. shop_reply is written by code: when next_step says to send it, send it exactly; never add a figure, a reason or an accusation. A screenshot is at most likely paid; never say received or verified.
A pasted bank SMS with no picture: parse_bank_alert reads it (amount, bank, reference, time; claims only), then check_payment with that shop and amount. Not in the feed: tell the sender it is not showing yet; never accuse anyone.
Check the bank feed with check_payment. If the bank connector is down, ask the owner with ask_question; never try the bank portal or the web instead.
The alert layouts the parser knows: references/bank-alert-formats.md.
