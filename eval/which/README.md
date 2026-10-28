# Two open asks on one thread: held-out set (RAI-272)

40 cases, written on 28 Oct 2026 before the fix and never used to tune it.
Each case opens a delivery receipt ask (Reply YES to confirm), a correction
question (Reply YES or NO, about a pending restock draft or credit request),
or both, on one shop thread. Then the shop replies, once or twice.

Expected outcome per reply:

* `which`: Mimi asks one short question written by code ("Yes to the
  delivery receipt or the order change?"). Nothing is bound: no Order turn, no
  consent, no change to the task, and both asks stay open.
* `receipt`: the reply goes to Order for the receipt. The correction stays open.
* `fix`: the reply answers the correction question. The receipt stays open.
* `owner`: still not clear after the question. The owner gets a note, the shop
  is told the owner will check. No second question.

Groups: A bare YES or NO with both open (15), B a yes or no with words that
say which (10), C the question and then the shop's answer (10), D controls
with one ask open (5).

Run: `python -m mimi.api.which_eval` (offline, no model; pass marks 95%
correct and 0 binds on an ambiguous reply).
