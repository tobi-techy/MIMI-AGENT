---
name: credit-request
description: The shop wants more credit: raise my limit, give me goods now and I pay later, credit bump, sometimes with a shelf photo.
helper: credit
version: 1.1.0
status: active
tier: approve
consent: [shop:balance, shop:payments, shop:promises, shop:messages, org:receivables, org:owner]
tools: [get_credit_file, ask_for_shelf_photo, count_shelf_photo, prepare_credit_review, submit_credit_review, send_to_user, ask_question, update_plan]
scripts: []
senders: [shop, rep]
money: true
handoffs: []
---

A shop asks for more credit (shop_ask):
1. get_credit_file. If it is denied, send the shop exactly: "{{needs_consent}}" (send_to_user, ends_turn=true) and stop. If open_review_id is set, send the shop already_waiting_reply and stop.
2. No photo yet: ask_for_shelf_photo, then end the turn with no message (the ask is the message).
3. The shop sent a photo (its message names a photo_id): count_shelf_photo. If it is not usable and next_step says ask again, call ask_for_shelf_photo with again=true and why, then end the turn. Ask again only once.
4. Then prepare_credit_review (with the photo_id if the count was usable and found items, else none). In one turn call both: send_to_user to the shop with shop_reply exactly as written (channel shop_whatsapp, ends_turn=true) and submit_credit_review with the same review_id, photo_id and origin, and a note_for_owner of one or two plain sentences.
