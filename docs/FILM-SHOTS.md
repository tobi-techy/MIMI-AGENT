# Freeze and film shot list (RAI-264/274)

Frozen beats, locked on the blessed `MIMI_SKILLS=1` path (skills order
held-out 47/47, photo routing fresh 27/27). All six pass live on the
Atlasflow demo; rehearse and film exactly as below.

Base: `https://mimi-agent-tobi-omotade-2cd167ac.atlasflow.dev`.
Health: `GET /v1/health`. Film takes run against the API; the console at
`https://demo.userail.money` is the click-through shell.

## House rules for every take

* One fresh session per take: `POST /v1/demo/sessions`, keep the token.
  A full six-beat run burns about ₦297 of the ₦400 daily per-address cap,
  so never film two takes on one session. After midnight Lagos time the
  cap resets.
* Shop turns use the full seed phone (`+2340000` plus six digits, e.g.
  shop-040 is `+2340000000139`). A short or wrong number lands on the
  onboarding thread instead of the shop.
* List-photo captions name nothing else ("my order for next van"). A
  caption that mentions payment, a complaint or a delivery routes away
  from Order; that is the known Order 3/47 miss, dodge it by discipline.
* Allow 8 to 15 seconds a turn; the live p95 is 11.6s. Pause the edit,
  do not rush the take.
* Synthetic Coral Lane world only. No WhatsApp adapter claims, no live
  bank claims: a screenshot is "likely paid until the bank confirms",
  never verified on camera.
* No em or en dashes in anything the camera reads; the style guard
  strips them from Mimi's replies, keep titles and cards the same.

## Shot 1: Pidgin YES receipt

1. `POST /v1/events` delivery_completed for shop-040 (Kunle Kiosk).
2. Wait for the receipt on the shop thread (ref dlv-56-9039, N64,250).
3. Shop turn from `+2340000000139`: "Yes, e don complete, I don check am".
4. Camera line: "Thank you. Delivery dlv-56-9039 is confirmed."
   (order/drop-confirmation).

## Shot 2: dispute grace

1. `POST /v1/events` delivery_completed for shop-008 (Yinka Provision
   Store, ref dlv-56-9007, N325,700).
2. Shop turn from `+2340000000107`: "Two carton short, na only part
   I receive".
3. Shop hears: "Thanks, I've told Coral Lane Trading about this
   delivery. Nothing has changed on your account." Owner side gets the
   dispute flag. No blame, no account change.

## Shot 3: voice promise readback plus YES

1. `POST /v1/events` delivery_completed for shop-023 (Mama Emeka Kiosk,
   ref dlv-56-9022, N97,600).
2. Shop voice turn from `+2340000000122` with transcript "Yes,
   everything don arrive, I go pay Friday".
3. Camera line: "Thank you. Delivery dlv-56-9022 is confirmed." Voice
   playback itself is verified in the console, not on the API.

## Shot 4: fake screenshot caught, genuine likely paid

1. Upload `eval/proof/screens/f08.jpg` via `POST /v1/media`
   (impossible-time fake, shop-035), then a shop turn from
   `+2340000000134` pointing at the ref with caption "I don pay, see
   receipt".
2. Camera: "Thanks, I've sent this to Coral Lane Trading to confirm
   with the bank. Nothing has changed on your account yet." Approvals
   card `flag_payment_proof` preview "looks_fake: time_real". Never
   accuse on camera.
3. Upload `eval/proof/screens/g02.jpg`, same shop, caption "I don pay
   part of my balance".
4. Camera: "Thanks. I can see N34,950 from Chidi Fagbemi on your
   screenshot. I've sent it to Coral Lane Trading to confirm with the
   bank. It shows as likely paid until the bank confirms."

## Shot 5: shelf photo credit card, owner declines

1. Credit ask from shop-020 (`+2340000000119`): "Abeg increase my
   credit, I need more stock for next week". Mimi asks for one clear
   shelf photo.
2. Upload `eval/credit/shelves/x01.jpg` via `POST /v1/media`, shop turn
   pointing at the ref with `camera_exif: true`, caption "my shelf
   photo, items facing camera".
3. Camera: the SAM count ("worth about N17,800") plus "I've sent your
   request with this to Coral Lane Trading. They decide the limit."
4. Owner: `GET /v1/approvals`, `POST
   /v1/approvals/{id}/reject` with `{"note": "...", "by": "owner"}`.
   Demo refuses limit-raise approves; the decline is the filmed
   decision. Thread shows the system rejection line.

## Shot 6: list-photo order priced, owner approves

1. Upload `eval/order/photos/ol-01.jpg` via `POST /v1/media`, shop turn
   from `+2340000000112` pointing at the ref, `camera_exif: true`,
   caption exactly "my order for next van". Expect route rule
   `photo_order_list_seen`, no model call.
2. Camera: the priced draft, 3 lines totalling N183,500, "I've sent it
   to Coral Lane Trading to approve."
3. Owner: `POST /v1/approvals/{id}/approve` with `{"note": "...",
   "by": "owner"}`.
4. Camera: "Coral Lane Trading approved your order ord-013-6c2c1f
   (N183,500). It comes with the next van."

## Not on camera

Stock slow-line cases (thin recall 72.3%), the kill switch toggle,
cap-breach refusals, credit-limit approve. Reads (`GET /v1/threads`,
`/v1/shops`, `/v1/credit`) keep working under every cap and kill state
and are safe B-roll.
