---
name: brand-email
description: The owner asks for an email or letter to a brand, manufacturer or bank (always a draft for the owner).
helper: brand
version: 1.1.0
status: active
tier: approve
consent: [org:owner]
tools: [draft_brand_email, get_brand_promos, send_to_user, ask_question]
scripts: []
senders: [owner]
money: false
handoffs: []
---

The owner asks for an email or letter to a brand or manufacturer: call draft_brand_email with a short, polite draft in the owner's voice and no naira figures, then tell the owner in one line on owner_console that the draft is in approvals. It is never sent by Mimi.
