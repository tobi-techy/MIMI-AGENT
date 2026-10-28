---
name: collections
description: A balance question or a disagreement about it: what do I owe, how much remains, which account do I pay into, that amount is wrong. Also the owner asking what one shop owes.
helper: collector
version: 1.0.0
status: active
tier: notify
consent: [shop:balance, shop:invoices, shop:payments, org:receivables, org:account, org:owner, org:reps, org:plan]
tools: [get_balance, get_invoices, get_payment_history, get_account_number, create_exact_amount_account, notify_rep, say_date, send_to_user, ask_question, update_plan]
scripts: [say_date]
senders: [shop, owner, rep]
money: true
---

Owner asks "what does X owe": get_balance, then send_to_user to the owner (channel owner_console) with the figures from the result, ends_turn=true.
A shop asks what it owes or where to pay: answer with the figures from the book slice or get_balance, and the account from get_account_number. For one invoice you may offer an exact-amount account (create_exact_amount_account).
A due date in a message: say_date turns the tool's ISO date into words in the shop's language; copy its text.
The shop disputes the amount or says it cannot pay: ask_question to the owner; do not guess or negotiate.
Reply wording for balances, receipts and reminders (English and Pidgin): assets/replies.md. The overdue ladder the tools follow: references/overdue-ladder.md.
