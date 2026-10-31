---
name: proofs-today
description: The owner asks which shops sent payment proofs today and how they compare with what each owes.
helper: proof
version: 1.0.0
status: draft
tier: auto
consent: [org:owner]
tools: []
scripts: []
senders: [owner]
money: true
planned_tools: [list_payment_proofs]
---

Goal: the owner sees today's payment proofs next to what each shop owes, at a glance.

Today this skill is answered by code, not by a model: a router rule picks it for the owner ("did any shop send a payment proof today?", "any fake alerts?"), and `mimi.api.cards` lists the payment screenshots Proof checked today (likely or flagged, never "paid") with each shop's amount owed and overdue from the book. Only the bank feed makes a payment verified; nothing here counts a payment, approves or sends anything.
