# Demo script (RAI-264), v1.2

The 3-minute video for the Meta Global AI Developer Hackathon. Hard limit
**3:00**, target **2:45**. It opens with one line on what existed before the
event and what was built this week (organizer ruling, 26 Oct 2026).

Every scene below uses the fictional Coral Lane Trading world: the shops,
people, phone numbers, brands and banks are all made up. The model calls are
real. Each scene's words, fixtures and Mimi's replies are the ones the
end-to-end run recorded on Muse Spark 1.3 and SAM 3.1 (`eval/e2e/`), so the
script quotes what Mimi actually said.

**v1.2 (4 Nov 2026) audits every scene against the live demo book.** The
shops, invoices and amounts below are the ones the live host answers with
today, not the ones the recording assumed. Scene 2 moved shop because of it:
read "Runnable on the live book" before you open a camera, and re-audit if the
book moves again.

Check it offline, for N0:

```bash
META_API_KEY= python scripts/demo_script_check.py   # e2e replay, then 16 checks on the quoted lines
```

## Runnable on the live book (audited 4 Nov 2026)

Audited by reading the hosted demo and the seed: `GET /v1/shops`,
`GET /v1/shops/{shop_id}`, `GET /v1/credit`, `GET /v1/routines`, and every
fixture in `eval/proof/screens.jsonl` compared with the shop's open invoices.
Reads only, no model calls. The live host is
`https://mimi-agent-tobi-omotade-2cd167ac.atlasflow.dev`; the console is
`https://demo.userail.money`. The host was redeployed on 4 Nov at about
10:38 UTC from main `d995e67` (holder commit `813c20d`, deployment
`01a1a623`), which is the build that carries the routines rail (operational
note 5). The book is the same seeded book either side of it: every demo
session gets its own fresh copy.

| # | Shop | Delivery / invoice | Amount | Runs on the live book? |
|---|---|---|---|---|
| 1 | shop-040 Kunle Kiosk `+2340000000139`; shop-008 Yinka Provision Store `+2340000000107` | `dlv-56-9039`, `dlv-56-9007`, both **live** and neither confirmed | receipt N64,250 and N325,700 | **Yes.** Both deliveries are in the live book with exactly the lines and totals quoted below, and neither has consent yet, which is what the receipt flow reads. |
| 2a fake | shop-035 Mama Chidi Kiosk `+2340000000134` | none: a fake never reaches matching | `f08` prints N34,950, dated "Sep 31, 2026" | **Yes.** The rule fails on the printed date before any invoice is touched, so the fact that shop-035 owes N0 does not matter here. |
| 2b real | **shop-005 Funke & Sons Mini Mart `+2340000000104`** | `dlv-00-0055` = **INV-00-0055**, open at 400,000 kobo (N4,000), the oldest invoice on the shop's book | `g18`: N4,000.00 from Funke Ogundipe | **Yes, and proved live on 4 Nov.** See "Scene 2". |
| 3 | shop-014 Folake Foodstuff & Provisions `+2340000000113` | `dlv-32-0159` carries 6 tub Mamaland Custard 2kg at N7,200 a tub | restock draft N43,200 | **Yes.** The custard line is in the live book at that unit price, and `stock.weekly_check` routes to `stock/stock-count` by rule on the deployed build. |
| 4 | shop-011 His Mercy Mini Mart `+2340000000110` | none: a credit review has no invoice | limit N640,000, owed N0 | **Yes, except the approve click**, which demo mode refuses on purpose. The limit and the N0 balance are what the live `/v1/credit` says today. |
| 5 | shop-001 Mama Sola Provisions | 14 open invoices, limit N1,700,000, over it by N1,797,300 | owes **N3,497,300** | **Yes.** The top debtor is still shop-001 and the figure is exact. The answer needs a live model call (see "Failures"). |
| 6 | end card | | | n/a |

**Why scene 2 moved.** `docs/DEMO-SCRIPT.md` v1.1 ran the real half on
shop-035, fixture `g02`, N34,950 against `dlv-56-9034`. On the live book
shop-035 **owes N0 and has no open invoice at all** (`GET /v1/shops/shop-035`
answers `outstanding_kobo: 0`, `receivables: []`), so `g02` would be read as
likely and then have nothing to be applied to. Of the 20 genuine fixtures in
`eval/proof/screens.jsonl`, **exactly one has an amount equal to an open
invoice's outstanding on the live book**: `g18`, N4,000, against
`dlv-00-0055`. Every other genuine screenshot's shop either owes nothing at
all (shop-035, shop-038, shop-019, shop-011, shop-003, shop-013, shop-031,
shop-024) or has no open invoice of that amount. So the real half of scene 2
runs on shop-005 or it does not run.

The N0 replay is unaffected: it replays the recorded e2e store, which was
built by the runner and is not the seeded live book. Only a live take needs
the change.

## Scene table

| # | Time | Scene | Seeded shop | What the shop side sends | What Mimi does (recorded) | Console screen | Offline check |
|---|---|---|---|---|---|---|---|
| 0 | 0:00 to 0:10 | Opening line | | | | Tobi on camera, or a title card | n/a |
| 1 | 0:10 to 0:40 | Delivery receipt: one shop confirms, one is short | shop-040 Kunle Kiosk; shop-008 Yinka Provision Store | `delivery_completed` events, then the shops' replies: "ok / gbogbo goods ti de" and "Mimi na only 4 ctn of that small sunrise reach / 2 ctn no come at all" | Receipt to each shop. shop-040 confirmed and its consent kept; shop-008 flagged to the owner in its own words, nothing recorded | Shop thread (phone size), then Threads list with the flag | **Pass** (6 checks) |
| 2 | 0:40 to 1:10 | Payment screenshot: a fake caught, then the real one "likely" | shop-035 Mama Chidi Kiosk (fake); **shop-005 Funke & Sons Mini Mart (real)** | Screenshot `f08` (dated "Sep 31, 2026") from shop-035, then `g18` (N4,000) from shop-005, both with "Mimi, mo ti transfer o, e check receipt abeg" | f08: `looks_fake` on `time_real`, to the owner, neutral note to the shop. g18: N4,000 read, likely paid until the bank confirms, against INV-00-0055 | Shop thread + the Proof card (pending) in approvals | **Pass** (3 checks) |
| 3 | 1:10 to 1:35 | Stock: one tap, then a voice correction mid-task | shop-014 Folake Foodstuff & Provisions | `stock.weekly_check` routine; tap "3-5"; voice note "abeg make am 6 tubs, no be 4" while the turn runs | One-tap question; one restock draft (v2) of 6 tubs of Mamaland Custard 2kg in approvals; one reply; nothing ordered | Shop thread + approvals card | **Pass** (3 checks) |
| 4 | 1:35 to 2:05 | Credit: shelf photo, SAM 3.1 count, owner decides | shop-011 His Mercy Mini Mart | Credit ask in Yoruba and Pidgin; shelf photo `s11` with "Mimi e wo o, na my shelf be dis" | Asks for one shelf photo; SAM 3.1 counts 11 + 5 + 4 packs (about N10,500); `support` from 13 of 13 bank-verified payments; draft to the owner, limit unchanged | Shop thread + Credit page / approvals | **Pass** (3 checks); approve click: see "Failures" |
| 5 | 2:05 to 2:35 | Owner view and numbers | owner | Owner types "Who owes me the most?" | Routes to Collector (`collector/chase`) by rule, no model call; the answer needs a live Muse Spark call | Mimi thread + numbers card | **Route passes**; answer not checkable offline (see "Failures") |
| 6 | 2:35 to 2:45 | End card | | | | Repo, demo link, "Data is synthetic. Model calls are real." | n/a |

Total **2:45**. Scene 5 can give up 5 seconds if a take runs long. Scene 2 has
two optional beats below ("the bank confirms", +20 s, and "a credit nobody
claimed", +15 s); the hard limit is 3:00, so at most one of them fits, and
only if something of similar length comes out of another scene first. Decide
that before the first take, not during it.

## Opening line (10 seconds)

> "Before this week, Mimi was a console prototype on sample data and a
> marketing site. Everything you see Mimi do here, the agents, the checks
> and every answer, we built this week on Meta's models."

About 30 words: read it at a normal pace and it lands at 10 seconds. On
screen while it plays: "Built 26 Oct to 4 Nov 2026" and "Console screens:
before the event".

## Scenes in detail

All five shops are in the seeded world from `python -m mimi.store seed`
(fixed seed). Phone numbers are the synthetic ones in the store; the sim sends
them so the front door finds the shop. Times are Africa/Lagos (WAT).

### Scene 1: delivery receipt (0:10 to 0:40, 30 s)

**Shops:** shop-040 Kunle Kiosk (`+2340000000139`), shop-008 Yinka Provision
Store (`+2340000000107`).

**The sim sends** (from the recorded e2e run, `mimi/api/e2e/runner.py`):

1. `POST /v1/events` `{"event_id": "e2e.dlv.dlv-56-9039", "kind": "webhook", "name": "delivery_completed", "shop_id": "shop-040"}`
2. `POST /v1/turns` `{"message_id": "e2e.shop-040.receipt.1", "phone": "+2340000000139", "text": "ok\ngbogbo goods ti de"}` (Yoruba: "all the goods have arrived")
3. The same for shop-008 (`dlv-56-9007`), with
   `"text": "Mimi na only 4 ctn of that small sunrise reach\n2 ctn no come at all"`

**Mimi replies:**

* To shop-040: "Coral Lane Trading delivery receipt for Kunle Kiosk / Ref
  dlv-56-9039, 2026-10-26 / 2 tub Mamaland Custard 2kg: N14,400 / 3 ctn
  Sunrise Instant Noodles 70g: N38,250 / 4 pk Ekiti Spring Water 75cl:
  N11,600 / Total: N64,250 / Reply YES to confirm." Then, after the reply:
  "Thank you. Delivery dlv-56-9039 is confirmed."
* To shop-008: the receipt (total N325,700), then "Thanks, I've told Coral
  Lane Trading about this delivery. Nothing has changed on your account."
* To the owner: `flag_delivery_to_owner`, "shop-008 about delivery
  dlv-56-9007: dispute. The shop said: ..." with the shop's own words.

**Live book:** `dlv-56-9039` is 2 tub Mamaland Custard 2kg, 3 ctn Sunrise
Instant Noodles 70g and 4 pk Ekiti Spring Water 75cl, total N64,250;
`dlv-56-9007` is 4 ctn Sunrise Big Pack Noodles 120g, 6 ctn Sunrise Instant
Noodles 70g, 3 tin Seaview Sardines 125g and 2 bag Amber Granulated Sugar
50kg, total N325,700. Both are on the live day and neither has consent, which
is what `get_open_deliveries` looks for. The receipt text is built by code
from those lines, so the totals cannot drift from the book.

**Console:** shop-040's thread at phone size (the receipt, the reply, the
confirmation), then Threads with Yinka Provision Store's flag and the
reasoning steps open on one row.

**Voiceover (about 60 words):**

> "A van drops goods at Kunle Kiosk. Mimi sends the receipt on WhatsApp.
> Kunle answers in Yoruba: all the goods have arrived. Muse Spark reads it,
> code checks the quote, and the confirmation is kept as Kunle's own
> consent. Yinka Provision Store says two cartons never came. Mimi records
> nothing and flags it to the owner, in the shop's own words."

### Scene 2: payment screenshot (0:40 to 1:10, 30 s)

**Shops:** shop-035 Mama Chidi Kiosk (`+2340000000134`) sends the fake;
**shop-005 Funke & Sons Mini Mart (`+2340000000104`)** sends the real one,
against its oldest open invoice, `dlv-00-0055` = INV-00-0055, N4,000.

**The sim sends** two image turns, each with
`"caption": "Mimi, mo ti transfer o, e check receipt abeg"`:

1. `demo.shop-035.proof.1`, received 13:30 WAT: `eval/proof/screens/f08.jpg`
   (fake: printed time "Sep 31, 2026 02:15:44 PM", a date that does not exist)
2. `demo.shop-005.proof.1`, received 16:59 WAT: `eval/proof/screens/g18.jpg`
   (genuine, N4,000.00 from Funke Ogundipe)

The media block is `{"kind": "image", "ref": "local:proof/screens/f08.jpg", "mime": "image/jpeg", "sha256": ..., "width": 540, "height": 1100, "camera_exif": false, "caption": ...}`, the hash taken from `eval/proof/screens.jsonl`.

**Mimi does:**

* f08 (shop-035): Proof reads it with Muse Spark vision; code fails
  `time_real` ("'Sep 31, 2026 02:15:44 PM' is not a real date and time").
  Verdict `looks_fake`, an approvals card for the owner ("Would send
  shop-035's screenshot to the owner (looks_fake: time_real)"), and to the
  shop only: "Thanks, I've sent this to Coral Lane Trading to confirm with
  the bank. Nothing has changed on your account yet."
* g18 (shop-005): "Thanks. I can see N4,000 from Funke Ogundipe on your
  screenshot. I've sent it to Coral Lane Trading to confirm with the bank.
  It shows as likely paid until the bank confirms." Matched to delivery
  `dlv-00-0055`, whose outstanding is exactly N4,000.

**Optional beat: the bank confirms (+20 s).** This is the only place in the
video where the money loop closes, so it is worth the time if you can find
it. `POST /v1/events` with
`{"event_id": "demo.bank.1", "kind": "webhook", "name": "bank_credit", "shop_id": "shop-005", "amount_ngn": 4000}`
(HTTP 202; the amount must be the whole naira on the screenshot). Then:

* The shop gets "We have received your N4,000 payment. Thank you."
* The owner thread gets "The bank confirmed Funke & Sons Mini Mart's N4,000
  transfer, so it is now verified. Approve it to record it in Zoho Books."
* `/approvals` holds the card: "Record Funke & Sons Mini Mart's N4,000 transfer
  in Zoho Books." **Film this screen now: it shows pending items only.**
* Approving it answers `{"demo": true, "undo_available": true, "zoho_payment_id": "zfake_..."}`,
  and the owner thread reads "Recorded Funke & Sons Mini Mart's N4,000 payment
  in Zoho Books against INV-00-0055. You can undo it." with an Undo button.
  INV-00-0055 goes 400,000 to 0 kobo, and the shop's outstanding falls from
  N2,211,200 to N2,207,200, exactly N4,000. The `zfake_` prefix is honest:
  the demo records into a fake Zoho and says so.

**Optional beat: a credit nobody claimed (+15 s, no model call).** The same
route with an amount no waiting screenshot has, for example `amount_ngn
250000`: HTTP 202, and the owner is told, verbatim, "A bank credit came in for
Funke & Sons Mini Mart (N250,000), but there is no screenshot payment waiting
to match it. Nothing has changed." Nothing moves: no invoice, no card. The
sentence is written by code (`mimi/api/demo_wiring.py`), so this beat costs no
model call at all.

**Console:** the shop thread, then the Proof card for f08 in approvals, then,
if you film the closing beat, the owner thread with the card and the Undo
button.

**Voiceover (about 65 words):**

> "Mama Chidi pays by transfer and sends a screenshot. Muse Spark copies
> every field off the image. Code checks them: this one is dated the 31st of
> September, a day that does not exist. It goes to the owner, and the shop
> gets a polite note, no blame. Funke & Sons sends a real one for four
> thousand naira. Mimi reads it as likely paid. Only the bank's own credit
> makes it verified, and the owner decides what happens next."

### Scene 3: stock with a voice correction (1:10 to 1:35, 25 s)

**Shop:** shop-014 Folake Foodstuff & Provisions (`+2340000000113`).

**The sim sends:**

1. `POST /v1/events` `{"event_id": "e2e.stock.shop-014.1", "kind": "routine", "name": "stock.weekly_check", "shop_id": "shop-014"}`
2. The tap: `{"message_id": "e2e.shop-014.stock.1", "phone": "+2340000000113", "text": "3-5", "button_id": "btn:stock/stock-count"}`
3. While that turn runs, the voice note:
   `{"message_id": "e2e.shop-014.stock.voice", "phone": "+2340000000113", "media": {"kind": "voice", "mime": "audio/ogg", "transcript": "abeg make am 6 tubs, no be 4"}}`

The voice fixture is a voice-note media block with the channel's transcript
(no audio file). The audio path itself (Spitch and Muse voice) is covered by
the 50-clip voice eval, not by this scene.

**Mimi does:** "Quick stock check: how many tubs of Mamaland Custard 2kg do
you have left? 0-2 | 3-5 | 6+". After the tap the restock draft is being
made; the voice note is absorbed at the next step, so there is one draft,
version 2, 6 tubs (N43,200, "nothing is ordered") in the owner's approvals,
and one reply: "Okay. I've changed it to 6 tubs. Coral Lane Trading will
confirm before delivery."

**Live book:** shop-014's delivery `dlv-32-0159` carries 6 tub Mamaland
Custard 2kg at N7,200 a tub, which is where the N43,200 comes from. The shop
owes N328,750 against a N670,000 limit, so a restock of this size is not over
the limit. The `routine` event routes by rule (`stock.weekly_check` ->
`stock/stock-count`, no model call for the route), so this scene does not
depend on the routines rail: the event is what wakes the helper. The rail is
filmable on the deployed host now, but it lists the daily briefing and the
aging scan, not a stock check (operational note 5).

**Console:** shop thread (tap buttons visible), then the restock card in
approvals: version 2, 6 tubs. Version 1 (4 tubs) never reaches the owner,
because the correction is absorbed inside the same turn. **Film the approvals
screen before approving: it shows pending items only.**

**Voiceover (about 50 words):**

> "Nobody keeps a stock book. Mimi estimates stock from what was delivered
> and asks one question with a tap. Folake taps 3 to 5, then sends a voice
> note while Mimi is still working: make it 6 tubs. Mimi takes it in at the
> next step. One draft, one reply, nothing ordered until the owner says yes."

### Scene 4: credit with a shelf photo (1:35 to 2:05, 30 s)

**Shop:** shop-011 His Mercy Mini Mart (`+2340000000110`).

**The sim sends:**

1. `{"message_id": "e2e.shop-011.credit.1", "phone": "+2340000000110", "text": "Kaaro Mimi, abeg una fit increase credit limit mi small? Oja n taja daadaa, mo wan buy more goods. Na Halima"}`
2. After Mimi's ask: `e2e.shop-011.credit.2`, image `eval/credit/shelves/s11.jpg`
   (1280 x 960, camera photo), caption "Mimi e wo o, na my shelf be dis",
   hash from `eval/credit/shelves.jsonl`.

**Mimi does:**

* "I can ask Coral Lane Trading for you. Please send one clear photo of your
  shelf, with the items you sell facing the camera."
* SAM 3.1 finds the packs; code maps them to delivered items and values
  them: "I counted about 11 Sunrise Instant Noodles 70g packets, 5 Sunrise
  Big Pack Noodles 120g packets and 4 Smile Toothpaste 140g tube boxes,
  worth about N10,500 at Coral Lane Trading prices. I've sent your request
  with this to Coral Lane Trading. They decide the limit."
* Approvals: `submit_credit_review`, recommendation **support**, high
  confidence: "His Mercy Mini Mart owes N0 on a N640,000 limit with 13 of 13
  verified payments on time. Shelf stock worth about N10,500 supports
  turnover." `changes_limit: false`, `moves_money: false`.

**Live book:** `/v1/credit` today says shop-011 has a N640,000 limit, owes N0
and has N640,000 available, which is the state the recorded line assumes. The
count of verified payments and the shelf value are read at the time of the
take, so read the card's own numbers off the screen rather than assuming the
recorded 13 of 13 and N10,500 still hold.

**Console:** shop thread with the shelf photo, then the Credit page or the
approvals card with the reasons. **The owner's click here is refused on the
hosted demo on purpose** (see "Failures").

**Voiceover (about 55 words):**

> "His Mercy Mini Mart asks for more credit, in Yoruba and Pidgin. Mimi asks
> for one shelf photo. SAM 3.1 finds every pack, and code matches them to
> what was delivered. The recommendation comes from bank-verified payments
> only: 13 of 13 on time. Mimi never changes a limit. The owner decides."

### Scene 5: owner view and numbers (2:05 to 2:35, 30 s)

**The owner types** in the console: "Who owes me the most?"
(`{"message_id": "...", "source": "owner_message", "text": "Who owes me the most?"}`)

**Mimi does:** routes to Collector, skill `collector/chase`, by a phrase
rule (layer `lexical`, no model call); Collector reads the book with its
tools and answers through the claims gate. The answer itself needs a live
Muse Spark call. What the live book says, for checking the take: the top
debtor is **Mama Sola Provisions (shop-001), N3,497,300 outstanding across 14
open invoices**, against a N1,700,000 limit, so N1,797,300 past it (audited
4 Nov 2026; the overdue part depends on the day).

**Console:** Mimi's thread with the thinking line and steps, then the
approvals list, then a numbers card.

**Numbers card** (only figures from saved runs; replace with `numbers.md`
once RAI-274 lands):

* End to end through the API: 27 of 27 scenes, 11 forced worker kills, 0
  lost jobs, 0 duplicate replies (`eval/e2e/e2e.report.md`)
* Payment screenshots: 16 of 16 fakes caught, 0 fakes marked likely
  (`eval/proof/`)
* Receipt replies: 0 false confirms on 84 cases (`eval/receipts/`)

Do not show a soak figure until the RAI-273 soak report exists.

**Voiceover (about 50 words):**

> "The owner sees all of it in one console. Who owes me the most? Collector
> reads the book, and every figure is checked against the records before it
> goes out. Approvals wait for the owner. Mimi proposes, code checks, the
> owner approves, and Mimi never moves money."

### Scene 6: end card (2:35 to 2:45, 10 s)

On screen: `github.com/tobi-techy/MIMI-AGENT`, the hosted demo link
(`https://demo.userail.money`), "Data is
synthetic. Model calls are real." and "Built on Muse Spark 1.3 and SAM 3.1".

**Voiceover:**

> "Mimi. Built this week on Muse Spark and SAM 3.1. The data is synthetic;
> the model calls are real."

## Operational notes for the recording

These are the things that cost a take if nobody says them out loud first.

1. **`/approvals` shows pending items only, so approve after filming that
   screen, not before.** Once a card is approved it leaves the approvals
   screen, and there is no history view there. The record afterwards lives on
   the owner thread ("Recorded Funke & Sons Mini Mart's N4,000 payment in
   Zoho Books against INV-00-0055. You can undo it.") with the Undo button.
   The API does keep the decided card (`GET /v1/approvals?status=all` returns
   it); the console simply does not show it.
2. **A shop that sends the identical screenshot twice gets no reply at all.**
   Deduplication swallows it: the second turn produces no message and no new
   payment row, and a bank credit for it is then refused as unmatched. If the
   recording needs a second exchange, send a **different amount or a different
   fixture**, never the same image again.
3. **Shop messages never appear in the console, by design.** A shop thread in
   the console shows the task's lifecycle (task from the shop, handed to the
   helper, the helper replied), not the shop's words or Mimi's reply; the
   wording is a rule of the demo, written down in `DEMO-STATUS.md`. So the
   shop's own words can only be shown on the channel (the simulated phone),
   and the console evidence for the loop is the **owner thread**.
4. **The budgets.** The hosted demo caps a visitor address at **N400 a Lagos
   day** and the whole demo at N2,000 (`POST /v1/demo/sessions` answers
   `ip_cap_kobo 40000`, `global_cap_kobo 200000`). Measured: one screenshot
   scene cost N27, and a rehearsal that ran two of them cost N54.91 in total,
   so a recording fits inside one address's day with room to spare. Browser
   tasks are bounded by **count, not money**: 3 per session and 8 per visitor
   address a day, and 7 of the 8 were still available when this was written,
   after the day's rehearsal. Spend at most one browser task in a take.
5. **The routines rail is on the deployed host, behind the owner key.** Since
   the 4 Nov 10:38 UTC deploy, `GET /v1/routines` answers **401** to an
   unauthenticated call (`{"detail":"a valid bearer key is required"}`), so
   **mint a demo session first**: `POST /v1/demo/sessions` answers 201 and
   returns the token the console keeps in
   `sessionStorage["mimi-console-session"]` as `{token, expiresAt}`, and that
   token reads the rail. On a fresh session it lists two routines, "Morning
   briefing" (every day at 8:00 AM) and "Aging scan" (every day at 8:30 AM),
   each with an empty `last_run_at`, `last_finding` "It has not run yet." and a
   `next_run_at` on the next day. That is the honest state of a fresh
   container, and it is filmable. `POST /v1/routines/morning_brief/pause`
   answers `{"id":"morning_brief","name":"Morning briefing","paused":true}` and
   the rail then carries the switch as a row with a `paused_reason` written as
   a sentence ("Checking the note."); `resume` puts it back. `all` (or `*`) is
   the kill switch: `POST /v1/routines/all/pause` answers
   `{"id":"all","name":"All routines","paused":true}`. A visitor's session
   shows no routine that has already run, so film the rail, the switch and its
   sentence, not a run, and do not promise one.
6. **The console paints its shell before its data.** Tiles appear with labels
   and no values for one to two seconds while eight reads land. Open the
   screen, count to two, then speak. Otherwise the first frame of a shot has
   empty figures in it.

## Offline check: what passed and what did not

Run on 30 Oct 2026 from commit `24376c4` (origin/main), offline, N0:
`META_API_KEY= python -m mimi.api.e2e --no-write` passed 27/27 scenes
(routing 100% over 43 turns, 0 safety violations, 0 duplicate replies), and
`scripts/demo_script_check.py` held 16 of 16 checks on the lines quoted
above (scenes 1 to 4 from the replayed store, scene 5's route on the
demo-mode API with the offline model). The replay's store is the runner's own,
not the seeded live book, so scene 2's live shop change does not move it.

### Failures and gaps

1. **Scene 1 is not a voice note.** Plan v1 had shop-040 answer "by Pidgin
   voice note". The recorded reply is typed Yoruba ("ok / gbogbo goods ti
   de"), and that is what passes offline. A voice-note receipt reply has no
   recorded Muse Spark verdict, so it cannot be replayed for N0. Options:
   film it as recorded (recommended), or record one live voice take
   (Spitch plus one verdict call, a few naira) and add it to the cache.
2. **Scene 5's answer cannot be checked offline.** The route is checked
   (Collector, by rule, no model call). The reply needs Muse Spark; offline
   the API answers with its holding line ("I'm running in offline demo mode,
   so a person will follow up."). It will be checked on the first live take
   against the figure above.
3. **The scene 4 approve click is refused on the hosted demo, on purpose.**
   Demo mode answers 403 to approving a credit review (`docs/API.md`, "Demo
   mode"); declining works. To film that approve click, use a run in live
   mode (below), not the hosted demo. The **Zoho payment** click in scene 2
   is not affected: approving one on the hosted demo answered HTTP 200 with
   `{"demo": true, "undo_available": true, "zoho_payment_id": "zfake_..."}`
   on 4 Nov.
4. **"One YES joins the shop".** There is no separate join step in the
   recorded run: the confirmed receipt is recorded as the shop's consent.
   The voiceover says that, not "joins".
5. **Two edges left on a payment bigger than the bill.** The demo's bank
   credit spreads a payment over the shop's open invoices, caps each
   application at what that invoice owes, records the excess on the shop's
   account (`mimi/store/on_account.py`, visible as `on_account_kobo` and
   `on_account_naira` on `GET /v1/shops`) and builds the Zoho card from what
   was **actually applied**, so the card can no longer claim more against an
   invoice than the invoice ever owed (PR #61, merged before this deploy).
   What remains, and is documented in `docs/RETURNS.md` ("A payment bigger
   than the shop owes"): Go's own writer (`mimi/api/week/go.py`,
   `GoSim._pay`) pays the single oldest open invoice and puts the rest on the
   account rather than spreading it over the next invoices, and the
   after-the-roll case is decided by the payment's day against the delivery's
   day, so a payment written while its delivery sat unbilled for more than a
   day and verified only after the roll cannot be placed by its day stamp, and
   the roll's credit and the payment's application can both count. Neither is
   reachable in the five scenes.
6. **A shop's duplicate screenshot is silent** (operational note 2), and the
   rehearsal that found it was on the live host, not in the replay store.

## How to film it

Two sources, both labelled "synthetic data" on screen:

* **Recorded store, N0 (screens of scenes 1 to 4).** Run the e2e replay
  with a fixed workdir and serve its store read-only to the console:
  `META_API_KEY= python -m mimi.api.e2e --no-write --workdir /tmp/e2e-film`,
  then `DATABASE_URL=sqlite:////tmp/e2e-film/e2e.db MIMI_MODE=live
  MIMI_WORKERS=0 MIMI_API_KEYS=coral-lane:owner:<a 16+ character key>
  python -m mimi.api serve --port 8000` (no worker, so nothing runs and
  nothing is spent; checked on 30 Oct: 17 threads with their steps and
  approvals read back). Point a console build that uses an owner key at
  `http://127.0.0.1:8000` (how the console's live build takes the key is in
  the console repo). In this store shop-011's credit request is already
  approved; film the click in a live run.
* **Live takes (scene 2's closing beat, scene 4's click, scene 5's answer,
  any retake).** The API on a laptop in live mode with `META_API_KEY`, an
  owner key for the console and a channel key for the simulator, driving the
  same messages as above. Set a filming ceiling first (proposal: N600 for two
  takes of every scene) and keep the e2e cache untouched.
* **The hosted demo (a live take with no setup at all).** `demo.userail.money`
  drives the same messages, inside one address's N400 day and 8 browser
  tasks. Scene 2 was proved end to end this way on 4 Nov, and it is the
  cheapest way to film the money loop closing. The routines rail reads there
  too, once you have a session: mint one with `POST /v1/demo/sessions` and the
  rail is the owner read that token opens (operational note 5).

On the hosted demo a visitor's session token also takes shop messages by
phone and system events, so the sim can drive it, inside the caps. One
session is one fresh copy of the book: a rehearsal cannot spoil the take that
follows it, and equally the balances you rehearsed against will not be the
ones you film.

## Shot list

| Shot | Frame | What must be visible |
|---|---|---|
| A. Opening | Tobi on camera or a title card, 1920 x 1080 | "Built 26 Oct to 4 Nov 2026"; "Console screens: before the event" |
| B. Shop side, each scene | Phone frame 390 x 844 inside the 1080p canvas, left third | Label **"simulated shop phone"** above the frame; "synthetic data" badge; shop name |
| C. Console, each scene | Desktop console at 1440 x 900, scaled into the right two thirds | "synthetic data" badge or the demo banner ("Live demo: your own copy of Coral Lane Trading"); thread steps open once per scene |
| D. Scene 2 close-up | Proof card, zoomed 150% | the failed rule in words ("not a real date and time"); "likely" on **g18**, never "paid"; if the closing beat is filmed, the owner thread's "Recorded ... against INV-00-0055" and the Undo button |
| E. Scene 3 close-up | Restock card | version 2, 6 tubs, "nothing is ordered" |
| F. Scene 4 close-up | Credit card | "support", bank-verified payments, "limit unchanged", the owner's buttons |
| G. Scene 5 | Mimi thread, then numbers card | thinking line; each number with its source file in small type |
| H. Architecture | `docs/architecture.png`, 3 to 4 s inside scene 5 or 6 | the key: built this week vs before the event |
| I. End card | Static, 10 s | repo URL, demo link, "Data is synthetic. Model calls are real." |

Record each shot twice (clean take plus backup). No WhatsApp screenshots:
the shop side is the simulator until a test number exists (RAI-263).
