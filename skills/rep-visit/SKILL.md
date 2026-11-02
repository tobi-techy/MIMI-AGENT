---
name: rep-visit
description: The owner assigns a shop to a rep to visit or check on today.
helper: collector
version: 1.0.0
status: active
tier: auto
consent: [org:receivables, shop:balance, shop:messages, org:reps, org:owner]
tools: [get_balance, notify_rep, send_to_user, ask_question]
scripts: []
senders: [owner]
money: false
handoffs: []
---

Goal: the right rep visits the right shop today, with what they need to know.

A router rule picks this for the owner ("assign Divine Favour Provisions to Tunde to visit today"). Collector reads the shop's balance with get_balance, tells the shop's rep with notify_rep (what to check and why, with figures copied from the result), and then tells the owner in one sentence which rep was told about which shop. If the named rep is not the shop's rep, say so to the owner; never invent a rep.
