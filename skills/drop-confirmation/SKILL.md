---
name: drop-confirmation
description: The shop or rep is answering about a delivery that happened: goods received, YES, short, damaged, wrong item or leaking.
helper: order
version: 1.1.0
status: active
tier: notify
consent: [shop:invoices, shop:messages, org:owner]
tools: [get_open_deliveries, send_delivery_receipt, read_receipt_reply, record_delivery_consent, flag_delivery_to_owner, ask_receipt_again, send_to_user, ask_question, update_plan]
scripts: []
senders: [shop, rep]
money: false
handoffs: []
---

A delivery was completed (webhook delivery_completed, or the delivery follow-up routine): get_open_deliveries for the shop, then send_delivery_receipt for the delivery it names and end the turn with no message (the receipt is the message).
The shop replies about a delivery (YES, received, something short or damaged): get_open_deliveries if you do not know the delivery_id, then read_receipt_reply, then do exactly its next_step in one turn. shop_reply is written by code: send it exactly.
Never take the rep's side or the shop's side; record both. Never mark a drop confirmed without the shop's own reply.
