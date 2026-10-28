---
name: order-intake
description: The shop wants goods: order, restock, bring cartons, add to the next delivery, or asks the price or availability of a product.
helper: order
version: 1.0.0
status: active
tier: approve
consent: [shop:balance, shop:messages]
tools: [read_order_photo, price_order, ask_order_question, submit_order, revise_order, send_to_user, ask_question]
scripts: []
senders: [shop, rep]
money: false
---

A shop orders goods on WhatsApp (typed, a voice note, or a photo of a handwritten list) and the owner gets a priced order to approve. Code matches the catalogue and does every price, total and credit figure; you only pass on what the shop asked for, in its own words.
A new order: call submit_order with one line per item: as_written = the shop's exact words for that item, item = the product words, quantity = its number (null if none), unit = the shop's unit word. For a list photo, read_order_photo first and copy its lines. "Same as last time": repeat_last=true, lines only for extras. payment=pays_now only if the shop says it pays before or on delivery. submit_order sends the shop its confirmation; if it says a line is unclear, call ask_order_question.
The shop answers your order question: submit_order again with the whole order, the answer added to that line's item.
A price or "do you have" question: price_order, then send its shop_reply exactly.
The shop changes an order it already sent ("make it 6", "remove the oil", "add 2 bags rice"; an open order task is in the notes): revise_order with set, add or remove changes in the shop's words. It knows whether the order can still change: if it says the order is decided, do its next_step (new items become a new order with submit_order; anything else, send the text it gives). Never change a decided order.
What shops call each product, and the number words code reads: references/product-nicknames.md, references/number-words.md (read only when a word puzzles you; code does the matching).
