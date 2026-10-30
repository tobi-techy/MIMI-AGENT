# Demo script (RAI-264), v1.1

The 3-minute video for the Meta Global AI Developer Hackathon. Hard limit
**3:00**, target **2:45**. It opens with one line on what existed before the
event and what was built this week (organizer ruling, 26 Oct 2026).

Every scene below uses the fictional Coral Lane Trading world: the shops,
people, phone numbers, brands and banks are all made up. The model calls are
real. Each scene's words, fixtures and Mimi's replies are the ones the
end-to-end run recorded on Muse Spark 1.3 and SAM 3.1 (`eval/e2e/`), so the
script quotes what Mimi actually said.

Check it offline, for N0:

```bash
META_API_KEY= python scripts/demo_script_check.py   # e2e replay, then 16 checks on the quoted lines
```

## Scene table

| # | Time | Scene | Seeded shop | What the shop side sends | What Mimi does (recorded) | Console screen | Offline check |
|---|---|---|---|---|---|---|---|
| 0 | 0:00 to 0:10 | Opening line | | | | Tobi on camera, or a title card | n/a |
| 1 | 0:10 to 0:40 | Delivery receipt: one shop confirms, one is short | shop-040 Kunle Kiosk; shop-008 Yinka Provision Store | `delivery_completed` events, then the shops' replies: "ok / gbogbo goods ti de" and "Mimi na only 4 ctn of that small sunrise reach / 2 ctn no come at all" | Receipt to each shop. shop-040 confirmed and its consent kept; shop-008 flagged to the owner in its own words, nothing recorded | Shop thread (phone size), then Threads list with the flag | **Pass** (6 checks) |
| 2 | 0:40 to 1:10 | Payment screenshot: a fake caught, then the real one "likely" | shop-035 Mama Chidi Kiosk | Screenshot `f08` (dated "Sep 31, 2026"), then `g02`, both with "Mimi, mo ti transfer o, e check receipt abeg" | f08: `looks_fake` on `time_real`, to the owner, neutral note to the shop. g02: N34,950 read, likely paid until the bank confirms | Shop thread + Proof card in approvals | **Pass** (3 checks) |
| 3 | 1:10 to 1:35 | Stock: one tap, then a voice correction mid-task | shop-014 Folake Foodstuff & Provisions | `stock.weekly_check` routine; tap "3-5"; voice note "abeg make am 6 tubs, no be 4" while the turn runs | One-tap question; one restock draft (v2) of 6 tubs of Mamaland Custard 2kg in approvals; one reply; nothing ordered | Shop thread + approvals card | **Pass** (3 checks) |
| 4 | 1:35 to 2:05 | Credit: shelf photo, SAM 3.1 count, owner decides | shop-011 His Mercy Mini Mart | Credit ask in Yoruba and Pidgin; shelf photo `s11` with "Mimi e wo o, na my shelf be dis" | Asks for one shelf photo; SAM 3.1 counts 11 + 5 + 4 packs (about N10,500); `support` from 13 of 13 bank-verified payments; draft to the owner, limit unchanged | Shop thread + Credit page / approvals | **Pass** (3 checks); approve click: see "Failures" |
| 5 | 2:05 to 2:35 | Owner view and numbers | owner | Owner types "Who owes me the most?" | Routes to Collector (`collector/chase`) by rule, no model call; the answer needs a live Muse Spark call | Mimi thread + numbers card | **Route passes**; answer not checkable offline (see "Failures") |
| 6 | 2:35 to 2:45 | End card | | | | Repo, demo link, "Data is synthetic. Model calls are real." | n/a |

Total **2:45**. Scene 5 can give up 5 seconds if a take runs long.

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

**Shop:** shop-035 Mama Chidi Kiosk (`+2340000000134`).

**The sim sends** two image turns, each with
`"caption": "Mimi, mo ti transfer o, e check receipt abeg"`:

1. `e2e.shop-035.proof.1`, received 13:30 WAT: `eval/proof/screens/f08.jpg`
   (fake: printed time "Sep 31, 2026 02:15:44 PM", a date that does not exist)
2. `e2e.shop-035.proof.2`, received 15:56 WAT: `eval/proof/screens/g02.jpg`
   (genuine)

The media block is `{"kind": "image", "ref": "local:proof/screens/f08.jpg", "mime": "image/jpeg", "sha256": ..., "width": 540, "height": 1100, "camera_exif": false, "caption": ...}`, the hash taken from `eval/proof/screens.jsonl`.

**Mimi does:**

* f08: Proof reads it with Muse Spark vision; code fails `time_real`
  ("'Sep 31, 2026 02:15:44 PM' is not a real date and time"). Verdict
  `looks_fake`, an approvals card for the owner ("Would send shop-035's
  screenshot to the owner (looks_fake: time_real)"), and to the shop only:
  "Thanks, I've sent this to Coral Lane Trading to confirm with the bank.
  Nothing has changed on your account yet."
* g02: "Thanks. I can see N34,950 from Chidi Fagbemi on your screenshot.
  I've sent it to Coral Lane Trading to confirm with the bank. It shows as
  likely paid until the bank confirms." Matched to delivery dlv-56-9034.

**Console:** shop thread, then the Proof card in approvals with the failed
rule in plain words.

**Voiceover (about 60 words):**

> "Mama Chidi pays by transfer and sends a screenshot. Muse Spark copies
> every field off the image. Code checks them: this one is dated the 31st of
> September, a day that does not exist. It goes to the owner, and the shop
> gets a polite note, no blame. The real one reads as likely paid. Only the
> bank's own credit makes it verified."

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

**Console:** shop thread (tap buttons visible), then the restock card in
approvals: version 2, 6 tubs. Version 1 (4 tubs) never reaches the owner,
because the correction is absorbed inside the same turn.

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

**Console:** shop thread with the shelf photo, then the Credit page or the
approvals card with the reasons, then the owner's click.

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
Muse Spark call. What the book says, for checking the take: the top debtor
in the seeded world is **Mama Sola Provisions (shop-001), N3,497,300
outstanding in total** (read from the store's receivables; the overdue part
depends on the day).

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
(placeholder until deployed: see `docs/DEPLOY-DEMO.md`), "Data is
synthetic. Model calls are real." and "Built on Muse Spark 1.3 and SAM 3.1".

**Voiceover:**

> "Mimi. Built this week on Muse Spark and SAM 3.1. The data is synthetic;
> the model calls are real."

## Offline check: what passed and what did not

Run on 30 Oct 2026 from commit `24376c4` (origin/main), offline, N0:
`META_API_KEY= python -m mimi.api.e2e --no-write` passed 27/27 scenes
(routing 100% over 43 turns, 0 safety violations, 0 duplicate replies), and
`scripts/demo_script_check.py` held 16 of 16 checks on the lines quoted
above (scenes 1 to 4 from the replayed store, scene 5's route on the
demo-mode API with the offline model).

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
   mode"); declining works. To film the approve click, use a run in live
   mode (below), not the hosted demo.
4. **"One YES joins the shop".** There is no separate join step in the
   recorded run: the confirmed receipt is recorded as the shop's consent.
   The voiceover says that, not "joins".
5. **Filming on the hosted demo hits the per-address cap.** At N100 a
   visitor address a day and roughly N20 to N60 a live scene (estimated from
   the e2e run's N895.63 for 27 scenes, priced on recorded tokens), one
   address gets two to four live scenes a day there.

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
* **Live takes (scene 4's click, scene 5's answer, any retake).** The API on
  a laptop in live mode with `META_API_KEY`, an owner key for the console and
  a channel key for the simulator, driving the same messages as above. Set a
  filming ceiling first (proposal: N600 for two takes of every scene) and
  keep the e2e cache untouched.

On the hosted demo a visitor's session token also takes shop messages by
phone and system events, so the sim can drive it, inside the caps.

## Shot list

| Shot | Frame | What must be visible |
|---|---|---|
| A. Opening | Tobi on camera or a title card, 1920 x 1080 | "Built 26 Oct to 4 Nov 2026"; "Console screens: before the event" |
| B. Shop side, each scene | Phone frame 390 x 844 inside the 1080p canvas, left third | Label **"simulated shop phone"** above the frame; "synthetic data" badge; shop name |
| C. Console, each scene | Desktop console at 1440 x 900, scaled into the right two thirds | "synthetic data" badge or the demo banner ("Live demo: your own copy of Coral Lane Trading"); thread steps open once per scene |
| D. Scene 2 close-up | Proof card, zoomed 150% | the failed rule in words ("not a real date and time"); "likely" on g02, never "paid" |
| E. Scene 3 close-up | Restock card | version 2, 6 tubs, "nothing is ordered" |
| F. Scene 4 close-up | Credit card | "support", 13 of 13 verified payments, "limit unchanged", the owner's buttons |
| G. Scene 5 | Mimi thread, then numbers card | thinking line; each number with its source file in small type |
| H. Architecture | `docs/architecture.png`, 3 to 4 s inside scene 5 or 6 | the key: built this week vs before the event |
| I. End card | Static, 10 s | repo URL, demo link, "Data is synthetic. Model calls are real." |

Record each shot twice (clean take plus backup). No WhatsApp screenshots:
the shop side is the simulator until a test number exists (RAI-263).
