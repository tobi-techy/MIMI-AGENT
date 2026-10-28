---
name: credit-review
description: The owner asks about a shop's risk or limit: should I give them more credit, is this shop safe, review credit limits.
helper: credit
version: 1.0.0
status: active
tier: approve
consent: [org:receivables, shop:balance, shop:payments, org:owner]
tools: [find_shop, get_credit_file, prepare_credit_review, submit_credit_review, find_credit_candidates, send_to_user, ask_question, update_plan]
scripts: []
senders: [owner]
money: true
---

The owner asks about a shop (owner_ask): find_shop by the name the owner used (if several match, ask_question the owner which one), get_credit_file, prepare_credit_review with origin owner_ask, then send_to_user to owner_console with the recommendation and its reasons, copying figures exactly. Do not submit unless the owner asks you to.
Weekly review routine (weekly_review): find_credit_candidates, then for each shop prepare_credit_review and submit_credit_review with origin weekly_review. Send no messages.
How the recommendation is made (code, credit-review-v0.1): references/how-review-works.md.
