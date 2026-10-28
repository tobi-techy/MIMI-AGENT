---
name: low-stock-report
description: The owner asks which shops are running low or what stock to push.
helper: stock
version: 1.1.0
status: active
tier: auto
consent: [shop:messages, org:owner]
tools: [get_stock_estimates, send_to_user, ask_question]
scripts: []
senders: [owner]
money: false
handoffs: []
---

The owner asks which shops are running low or what stock to push: get_stock_estimates for the shops in question and tell the owner on owner_console which products look low, in plain words. Never share a shop's own counts with the distributor beyond what the estimates show; never place an order.
