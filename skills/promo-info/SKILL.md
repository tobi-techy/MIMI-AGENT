---
name: promo-info
description: Questions about brand promos, discounts from the manufacturer, new products, posters or campaigns.
helper: brand
version: 1.1.0
status: active
tier: auto
consent: [org:owner, shop:messages]
tools: [get_brand_promos, send_to_user, ask_question]
scripts: []
senders: [shop, rep, owner]
money: false
handoffs: []
---

A shop, rep or the owner asks about a promo, an offer, a free carton or a poster: call get_brand_promos (the brand they named, or empty), then send_to_user its shop_reply exactly to the person who asked, and end the turn.
