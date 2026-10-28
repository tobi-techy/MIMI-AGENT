---
name: escalate-owner
description: A complaint about a rep or the service, a threat, a legal or personal matter, or anything Mimi should not handle alone: the owner decides.
helper: mimi
version: 1.0.0
status: active
tier: auto
consent: [org:owner, shop:messages]
tools: [send_to_user, ask_question]
scripts: []
senders: [shop, rep]
money: false
---

A complaint, a threat or a legal or personal matter: thank them, say the owner will look at it, and tell the owner with send_to_user on owner_console.
