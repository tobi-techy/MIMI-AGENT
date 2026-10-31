---
name: reorder
description: The owner asks to reorder or restock for a shop, or what to restock first.
helper: order
version: 1.0.0
status: draft
tier: auto
consent: [org:owner]
tools: []
scripts: []
senders: [owner]
money: false
planned_tools: [get_last_order]
---

Goal: the owner sees what a reorder would hold before anyone places it.

Today this skill is answered by code, not by a model: a router rule picks it for the owner ("reorder for Mama Sola", "same as last time for X", "what should I restock first?"), and `mimi.api.cards` builds a reorder card from the book. With a shop named: that shop's last delivered order, each line priced at today's catalogue price, the total, and whether the order would push the shop past its credit limit. With no shop named: the items the stock estimator says will run out first, with a restock quantity worked out in code. Every figure is summed in code. Nothing is ordered, approved or sent from here.
