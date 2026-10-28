---
name: promise-to-pay
description: The shop says when it will pay or asks for more time: I will pay Friday, give me till month end, money will come next week.
helper: collector
version: 1.0.0
status: active
tier: notify
consent: [shop:balance, shop:invoices, shop:promises, shop:messages, org:plan, org:owner]
tools: [get_balance, get_invoices, set_followup, followup_hours, say_date, send_to_user, ask_question, update_plan]
scripts: [followup_hours, say_date]
senders: [shop, rep]
money: true
---

A promise to pay ("I will pay Friday", "give me till month end"): call followup_hours with the shop's own words for the day. Then set_followup with its in_hours and a short reason, and acknowledge the promise to the shop in one line using its said_as words. End the turn.
followup_hours does the date sums (Africa/Lagos, the day after the promised day); never count days or hours yourself. If it says the day is unclear, ask the shop one short question with ask_question instead.
Day words it understands (English, Pidgin, Yoruba, Hausa, Igbo): references/day-words.md.
