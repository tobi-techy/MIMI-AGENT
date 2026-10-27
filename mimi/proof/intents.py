"""Typed proposals for payment screenshots. None moves money.

* ``RecordPaymentProof``: the screenshot passed every check, so the payment
  is *likely*. The level is fixed to ``"likely"`` in the type: a screenshot
  can never propose anything stronger.
* ``ProofForApproval``: an item for the owner's approvals (a screenshot that
  looks fake, one Mimi still could not read, or a likely one the bank never
  showed), with every failed rule named. The owner decides; Mimi never
  accuses the shop.
* ``RecordBankWitness``: the distributor's own bank feed shows the credit,
  so all three witnesses agree (the rep logged the delivery, the shop sent
  proof, the bank received the money). Only this makes a payment verified.

Shop messages reuse ``SendShopMessage`` (``mimi.receipts.intents``) with a
``proof_*`` purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from mimi.receipts.intents import RECEIPT_INTENTS
from mimi.runtime.outbox import intent_types
from mimi.types import ShopScope

ApprovalReason = Literal["looks_fake", "unreadable_again", "no_bank_credit"]


@dataclass(frozen=True)
class RecordPaymentProof:
    scope: ShopScope
    proof_id: str
    amount_kobo: int
    # The invoice(s) it most likely pays, oldest first, and how it matched.
    delivery_ids: tuple[str, ...]
    match_kind: str
    open_kobo: int
    paid_at: str  # ISO, Africa/Lagos, as printed on the screenshot
    bank: str
    sender: str
    reference: str
    session_id: str
    image_sha256: str
    # "rule:result" for every check, so Go and the console can show them.
    checks: tuple[str, ...] = ()
    witnesses: tuple[str, ...] = ()
    level: Literal["likely"] = "likely"
    needs: Literal["bank_witness"] = "bank_witness"
    kind: Literal["record_payment_proof"] = field(default="record_payment_proof")


@dataclass(frozen=True)
class ProofForApproval:
    scope: ShopScope
    proof_id: str
    reason: ApprovalReason
    # Each failed rule, named, with its detail ("recipient: paid to ...").
    failed_rules: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    # What the screenshot shows, read in code; a claim, not a payment.
    amount_kobo_as_shown: int | None = None
    paid_at_as_shown: str = ""
    bank_as_shown: str = ""
    sender_as_shown: str = ""
    reference_as_shown: str = ""
    image_sha256: str = ""
    shop_message_id: str = ""
    kind: Literal["proof_for_approval"] = field(default="proof_for_approval")


@dataclass(frozen=True)
class RecordBankWitness:
    scope: ShopScope
    proof_id: str
    credit_id: str
    amount_kobo: int
    credited_at: str
    delivery_ids: tuple[str, ...]
    # rep:<rep_id>:<delivery_id>, shop:<message_id>, bank:<credit_id>
    witnesses: tuple[str, str, str]
    level: Literal["verified"] = "verified"
    kind: Literal["record_bank_witness"] = field(default="record_bank_witness")


PROOF_INTENTS: tuple[type, ...] = intent_types(
    RecordPaymentProof, ProofForApproval, RecordBankWitness
)
# The receipt flow's intents plus these, for a runner that runs both helpers.
ALL_INTENTS: tuple[type, ...] = tuple(dict.fromkeys((*RECEIPT_INTENTS, *PROOF_INTENTS)))
