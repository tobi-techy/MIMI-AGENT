---
name: bank-credit
description: A bank-feed credit arrived and must be matched to a shop and invoice.
helper: proof
version: 1.0.0
status: active
tier: notify
consent: [shop:payments, org:owner, org:plan]
tools: [check_payment, get_payment_history, notify_owner, send_to_user, ask_question]
scripts: []
senders: [system]
money: true
---

A bank-feed credit arrived: check_payment for the shop and amount it names to match it to an open invoice. A match needs nothing more; anything that does not match one shop and one invoice goes to the owner with notify_owner. Send nothing to a shop from this routine.
If the bank connector is down, ask the owner with ask_question; never try the bank portal or the web instead.
