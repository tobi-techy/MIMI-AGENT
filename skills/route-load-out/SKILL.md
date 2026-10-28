---
name: route-load-out
description: Morning. Build each rep's van load-out list and visit list, or a rep asks what to carry today.
helper: order
version: 1.0.0
status: draft
tier: auto
consent: []
tools: []
scripts: [sum_load_out]
senders: [rep, system]
money: false
planned_tools: [get_orders, get_route_shops, get_stock_levels, get_balances, send_to_rep, record_load_out]
---

Goal: each rep leaves the warehouse with the right stock for today's orders and a clear list of who to visit, who owes and who is low.
1. At 06:30 WAT, for each rep: get_orders for today's route and get_route_shops.
2. sum_load_out adds products by sku in code; add buffer stock only if the distributor's addendum sets one.
3. Visit order: shops with orders, shops that owe (get_balances), shops likely low (get_stock_levels), shops not visited in 7 days.
4. send_to_rep the load-out list and visit list. When the store keeper or rep confirms what was actually loaded, record_load_out. Differences are recorded, not corrected.
Never change an order without the shop or rep confirming. Never share one shop's balance with another shop.
Message wording: assets/replies.md.
