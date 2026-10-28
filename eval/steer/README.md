# Steer held-out set (RAI-301)

`heldout.jsonl`: 53 synthetic cases, written and committed before the steer
classifier existed. Do not tune on it after a run has been seen more than
once; record new cases in a separate dev file instead.

Every case is one Coral Lane shop (fictional) or the owner, a set-up and the
message(s) under test. 18 cases carry a voice note, a payment screenshot, a
shelf photo or a document.

## Fields

* `kind`: `multi_part`, `mid_task`, `correction`, `ambiguous`, `document`.
* `lang`: `en`, `pcm` (Pidgin), `yo-en` (Yoruba-flavoured English).
* `setup`:
  * `ask: receipt`: a delivery receipt ask is open on the thread (sent one hour before).
  * `running`: a turn of this thread is running (`skill`, the `text` that started
    it, its `source`) when the message lands. The message reaches the inbox and
    must be read at the next step boundary.
  * `tasks`: open work on the thread: `draft_restock` (`pending`, `approved`,
    `rejected`, with its `quantity`), `submit_credit_review` (`pending`),
    `set_followup` (a promised payment `day`), `record_delivery_consent` (`applied`).
* `messages`: one inbound message each (a burst when there are several):
  * `text`
  * `voice_transcript`: a voice note whose transcript the channel already made (synthetic text)
  * `voice_clip`: an `eval/voice/clips` file, transcribed through `mimi/voice`
    (recorded Spitch transcript, replayed)
  * `screenshot`: an `eval/proof/screens` fixture, with an optional `caption`
  * `shelf_photo`: an `eval/credit/shelves` fixture, with an optional `caption`
  * `document`: a synthetic PDF (name and caption only; there is no document reader)
* `expect`: the multiset of places that answer the message(s), each exactly once:
  * a helper name (`order`, `collector`, `credit`, `stock`, `proof`, `mimi`): a turn
    routed there answers it
  * `stock:revise`: the stock helper is woken to make a new version of the pending draft
  * `credit:adds`: the credit helper gets the new detail for its pending item
  * `owner`: a decided item was corrected; the owner is told, nothing is changed
  * `question`: one short clarifying question, nothing changed yet
  * `withdraw`: a pending item is withdrawn
  * `absorbed`: the running turn took the message in (no separate answer)
  * `cancel`: the running turn was cancelled, nothing it proposed applied
  * `released`: the message waited and ran as its own turn after the running one
* `also_ok`: other acceptable `expect` lists.

## Scoring (code)

A case passes when the answering places equal `expect` (or an `also_ok` list)
as a multiset. Held at zero over the whole set, whatever the score:

* lost messages: an inbound message whose turn is not done, or merged into a done turn
* parts with no recorded disposition
* duplicate replies: a turn that sends the shop more than one answer, or the same
  place answering twice where `expect` has it once
* money moves: any intent with a money verb
* edits to decided approvals: an approved or rejected approval whose status,
  payload or draft changed

Pass mark: 90% of cases.
