---
name: delivery-status
description: Goods not arrived yet: when is the van coming, where is my order, delivery late.
helper: order
version: 1.0.0
status: active
tier: auto
consent: [shop:invoices, shop:messages, org:owner]
tools: [get_open_deliveries, send_to_user, ask_question]
scripts: []
senders: [shop, rep, owner]
money: false
---

Goods not arrived yet ("when is the van coming", "where is my order"): say plainly in one line that the distributor's rep will help with the delivery time, with send_to_user, and end the turn. Never promise a time.
