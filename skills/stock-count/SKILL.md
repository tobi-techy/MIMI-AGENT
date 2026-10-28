---
name: stock-count
description: The shop says what is left on its shelf or answers a stock count (0-2, 3-5, 6+, it has finished, two cartons remain), or sends a shelf photo to count.
helper: stock
version: 1.1.0
status: active
tier: approve
consent: [shop:messages, org:owner]
tools: [get_stock_estimates, ask_stock_count, read_stock_answer, record_stock_count, draft_restock, revise_restock, send_to_user, ask_question, update_plan]
scripts: []
senders: [shop, rep]
money: false
handoffs: []
---

Weekly stock check (routine stock.weekly_check, or a shop thread with no open question): get_stock_estimates for the shop. If ask_sku is set, ask_stock_count for it and end the turn with no message (the ask is the message). Otherwise end the turn with no message.
A shop answers the open question (a tap 0-2, 3-5 or 6+, a number or words): read_stock_answer with the open sku (get_stock_estimates gives open_question_sku when you do not know it), then do exactly its next_step. shop_reply is written by code: when next_step says to send it, send it exactly.
A shop corrects its restock draft (a note says so; "make it 6 cartons, not 4", typed or in a voice note), whether the draft is pending or the one you are making now: revise_restock with the sku and the number the shop gave, instead of draft_restock. Still record_stock_count when you read a count. revise_restock sends the shop its notice itself: do not send it again. If the shop gave no clear number, ask_question.
