---
name: day-end-reconciliation
description: End of day. Compare each van's stock, cash and transfers against shop confirmations and flag gaps.
helper: proof
version: 1.0.0
status: draft
tier: notify
consent: []
tools: []
scripts: [reconcile_day]
senders: [system]
money: false
planned_tools: [get_load_out, get_returns, get_confirmations, get_payments, get_cash_declared, record_gap, notify_owner]
---

Goal: by 20:00 WAT the owner knows, per rep, whether stock and money add up, and where they don't, with evidence.
1. For each rep, reconcile_day does the sums in code: loaded minus returned is what should have been dropped; compare with confirmed drops; cash declared plus verified transfers against what shops confirmed paying.
2. Unconfirmed and disputed drops are listed separately, not counted as gaps yet.
3. A difference over the distributor's tolerance (default N1,000 or 1 carton) is a gap: record_gap with the shops, items, amounts and messages behind it.
4. Send the summary to the owner (or hold it for the owner brief).
Never call a gap theft. Never share one rep's gaps with another rep. Never count screenshots as payments.
Message wording: assets/replies.md.
