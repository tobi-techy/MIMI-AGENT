---
name: open-orders
description: The owner asks which shop orders are open or waiting now: any orders, new orders, orders waiting for approval.
helper: order
version: 1.0.0
status: draft
tier: auto
consent: [org:owner]
tools: []
scripts: []
senders: [owner]
money: false
planned_tools: [get_open_orders]
---

Goal: the owner sees, at a glance, which shop orders are waiting and what they hold.

Today this skill is answered by code, not by a model: a router rule picks it for the owner ("any orders now?", "show me the orders"), and `mimi.api.cards` reads the open orders from the book (orders waiting in approvals, newest first) into a list card with one sentence and next-step chips (docs/HANDOFFS.md). Every figure on the card is summed in code. Nothing is approved, changed or sent from here; the owner decides each order in approvals.
