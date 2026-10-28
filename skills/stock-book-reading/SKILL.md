---
name: stock-book-reading
description: A shop sends a photo of its stock book, an invoice, or a delivery note.
helper: stock
version: 1.0.0
status: draft
tier: notify
consent: []
tools: []
scripts: [check_stock_sums]
senders: [shop, rep]
money: false
planned_tools: [read_page_twice, get_shop_vocab, save_stock_count, ask_shop]
---

Goal: turn a photo into correct stock counts without making the shop type, and ask only about what is truly unclear.
1. Blurry, cut off or too dark: ask for a retake once, with a short tip ("hold the phone flat, in good light").
2. read_page_twice; check_stock_sums compares the two reads and checks the totals in code. Rows where both reads agree and sums hold are accepted.
3. Local names and units ("ctn", "pk", "Indomie small"): get_shop_vocab, and references in order-intake.
4. For unsure rows only, ask the shop in one message, at most 5 rows, as a numbered list they can answer with numbers.
5. save_stock_count with the photo reference, marked verified or self-reported.
Never invent a product or a number. Never ask the shop to retype the page. Never do arithmetic in the model.
Message wording: assets/replies.md.
