"""Code guards around the model's verdict. A wrong confirm is worse than a
wrong flag, so every doubt falls toward asking or flagging.

* Consent and the witness are recorded only when the outcome is
  ``confirmed``, confidence is at least ``CONFIRM_THRESHOLD`` (0.8), the
  verdict lists no disputed item, and ``evidence_quote`` is a real substring
  of one of the shop's own messages (checked here). The witness is the
  message the quote came from.
* A quote that carries role or instruction markers ("SYSTEM:", "ignore ...
  instructions") never confirms, whatever the model said. This guard can only
  stop a confirm, never cause one.
* ``disputed`` or ``partial`` always flags the owner with the quote, and no
  consent is recorded (for a partial, not even for the lines the shop did
  confirm: the owner settles the receipt).
* A voice note is judged from its transcript, and the quote must be in the
  transcript. A confirm is held back when its witness is a voice note whose
  transcript is ``low`` or ``failed``, or when any message in the new burst
  is such a voice note (Mimi cannot know what it said). Those go to the
  clarifying path and Mimi asks the shop to type.
* Anything else (unclear, unrelated, a confirm that failed a guard, a reply
  that did not parse) gets one clarifying message, then an owner flag.
* Quantities in a flag come from the receipt. The shop's stated number is
  carried only as its claim.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from mimi.receipts.inbox import ShopReply
from mimi.receipts.verdict import ReceiptVerdict
from mimi.world.model import Delivery

CONFIRM_THRESHOLD = 0.8
Action = Literal["consent", "flag_dispute", "clarify", "flag_unclear"]

_INJECTION = re.compile(
    r"(\b(system|assistant|developer)\s*:|ignore (all |your |previous |the )*"
    r"(instructions|rules)|\"outcome\"|</?\s*shop_data|mark (all|every)\b)",
    re.I,
)
_STOP = frozenset({"of", "the", "ctn", "pk", "tin", "bag", "btl", "keg", "tub", "and"})


@dataclass(frozen=True)
class DisputedLine:
    """A disputed receipt line. ``receipt_qty`` and the name come from the
    receipt; ``claimed_qty`` is only what the shop said."""

    as_written: str
    line_no: int | None = None
    sku: str = ""
    name: str = ""
    unit: str = ""
    receipt_qty: int | None = None
    claimed_qty: int | None = None


@dataclass(frozen=True)
class Decision:
    action: Action
    verdict: ReceiptVerdict
    witness: ShopReply | None = None
    evidence_found: bool = False
    # Why a confirmed verdict was not trusted ("" when it was, or was not one).
    held_back: str = ""
    disputed_lines: tuple[DisputedLine, ...] = ()


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split()).casefold()


def _fold(text: str) -> str:
    """``_norm`` with tone marks and dots below removed (ẹ -> e, à -> a)."""
    decomposed = unicodedata.normalize("NFD", _norm(text))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def find_witness(quote: str, replies: Sequence[ShopReply]) -> ShopReply | None:
    """The newest shop message that contains ``quote``.

    Tried in order: exactly; with whitespace and case normalised; then with
    diacritics folded, because the model sometimes miscopies Yoruba and Igbo
    tone marks ("biàra" for "bịara", seen in a live run). Folding only drops
    marks: every letter of the quote must still be in the shop's message.
    """
    q = quote.strip().strip('"').strip("'").strip()
    if not q:
        return None
    for match in (lambda t: q in t, lambda t: _norm(q) in _norm(t)):
        for r in reversed(replies):
            if match(r.text):
                return r
    fq = _fold(q)
    if not fq:
        return None
    for r in reversed(replies):
        if fq in _fold(r.text):
            return r
    return None


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", _norm(text)) if t not in _STOP}


def resolve_lines(
    verdict: ReceiptVerdict, delivery: Delivery
) -> tuple[DisputedLine, ...]:
    out: list[DisputedLine] = []
    for item in verdict.disputed_items:
        idx: int | None = None
        if item.receipt_line is not None and 1 <= item.receipt_line <= len(
            delivery.lines
        ):
            idx = item.receipt_line - 1
        else:
            want = _tokens(item.sku_or_name_as_written)
            best = 0
            for i, line in enumerate(delivery.lines):
                score = len(want & (_tokens(line.name) | _tokens(line.sku)))
                if score > best:
                    idx, best = i, score
        if idx is None:
            out.append(
                DisputedLine(
                    item.sku_or_name_as_written, claimed_qty=item.claimed_qty_if_stated
                )
            )
            continue
        line = delivery.lines[idx]
        out.append(
            DisputedLine(
                as_written=item.sku_or_name_as_written,
                line_no=idx + 1,
                sku=line.sku,
                name=line.name,
                unit=line.unit,
                receipt_qty=line.quantity,
                claimed_qty=item.claimed_qty_if_stated,
            )
        )
    return tuple(out)


def decide(
    verdict: ReceiptVerdict,
    *,
    delivery: Delivery,
    replies: Sequence[ShopReply],
    clarified: bool,
    threshold: float = CONFIRM_THRESHOLD,
    new_replies: Sequence[ShopReply] | None = None,
) -> Decision:
    """``new_replies``: the burst being judged (default: all ``replies``)."""
    burst = list(new_replies) if new_replies is not None else list(replies)
    witness = find_witness(verdict.evidence_quote, replies)
    found = witness is not None
    if verdict.outcome in ("disputed", "partial"):
        return Decision(
            "flag_dispute",
            verdict,
            witness,
            found,
            disputed_lines=resolve_lines(verdict, delivery),
        )
    held = ""
    if verdict.outcome == "confirmed":
        if verdict.disputed_items:
            # Says confirmed but lists a dispute: treat it as a dispute.
            return Decision(
                "flag_dispute",
                verdict,
                witness,
                found,
                held_back="confirmed_with_disputed_items",
                disputed_lines=resolve_lines(verdict, delivery),
            )
        if verdict.confidence < threshold:
            held = "low_confidence"
        elif witness is None:
            held = "evidence_not_in_shop_messages"
        elif witness.voice_doubtful:
            held = "low_quality_transcript"
        elif any(r.voice_doubtful for r in burst):
            held = "doubtful_voice_note_in_burst"
        elif _INJECTION.search(verdict.evidence_quote) or _INJECTION.search(
            witness.text
        ):
            held = "instruction_text_in_evidence"
        else:
            return Decision("consent", verdict, witness, True)
    action: Action = "flag_unclear" if clarified else "clarify"
    return Decision(action, verdict, witness, found, held_back=held)
