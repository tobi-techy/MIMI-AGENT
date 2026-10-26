"""Typed outbound intents for the delivery receipt flow.

Each is a frozen dataclass with a ``kind``, registered with the runtime via
``outbox.intent_types`` (which refuses money verbs). They are proposals:
the system of record sends the message, records the consent or raises the
flag. None moves money, and every figure in them is computed by code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from mimi.receipts.decide import DisputedLine
from mimi.runtime.outbox import intent_types
from mimi.types import ShopScope

MessagePurpose = Literal["delivery_receipt", "receipt_clarify", "receipt_type_request"]
FlagReason = Literal[
    "dispute", "partial_dispute", "unclear_reply", "no_reply", "voice_unreadable"
]


@dataclass(frozen=True)
class SendShopMessage:
    scope: ShopScope
    delivery_id: str
    purpose: MessagePurpose
    text: str
    # The receipt's figures, from code, so Go can check the text against them.
    total_kobo: int = 0
    kind: Literal["send_shop_message"] = field(default="send_shop_message")


@dataclass(frozen=True)
class RecordDeliveryConsent:
    """The shop confirmed the receipt: consent plus the witness.

    Proposed only after the code guards in ``decide.py`` pass. The witness is
    the shop's own message that contains ``evidence_quote``.
    """

    scope: ShopScope
    delivery_id: str
    receipt_digest: str
    total_kobo: int
    witness_message_id: str
    witness_sender: str
    witness_text: str
    witnessed_at: str
    evidence_quote: str = ""
    language: str = ""
    confidence: float = 0.0
    # "voice" when the witness is a voice note's transcript; then the
    # provider that transcribed it (the audio is not kept).
    witness_kind: str = "text"
    transcript_provider: str = ""
    consent: Literal["shop_confirmed"] = "shop_confirmed"
    kind: Literal["record_delivery_consent"] = field(default="record_delivery_consent")


@dataclass(frozen=True)
class FlagToOwner:
    scope: ShopScope
    delivery_id: str
    reason: FlagReason
    # The shop's own words, quoted, never paraphrased by a model. A voice
    # note appears as "[voice note transcript] ..." so the owner knows it
    # was heard, not typed.
    shop_words: str = ""
    # The span the model pointed at, checked in code to be in shop_words's
    # messages ("" when it was not found there).
    evidence_quote: str = ""
    # Receipt lines in dispute: names and quantities from the receipt; the
    # shop's stated number only as its claim.
    disputed_lines: tuple[DisputedLine, ...] = ()
    language: str = ""
    confidence: float = 0.0
    model_reason: str = ""
    kind: Literal["flag_to_owner"] = field(default="flag_to_owner")


RECEIPT_INTENTS: tuple[type, ...] = intent_types(
    SendShopMessage, RecordDeliveryConsent, FlagToOwner
)
