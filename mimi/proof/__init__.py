"""Payment screenshots (RAI-268/269): Muse Spark reads, code checks, likely match.

A shop's screenshot of a transfer is read into fields by Muse Spark
(``reading.py``), checked in code (``checks.py``) and matched to an open
invoice. It is only ever "likely paid": a payment is verified only when the
distributor's own bank feed shows the credit (``witness.py``). A screenshot
that looks wrong goes to the owner's approvals; Mimi never accuses the shop
and never moves money.
"""

from mimi.proof.checks import Assessment, OpenInvoice, assess
from mimi.proof.flow import PROOF_HELPER, ImageWebhook, ProofDesk, ProofFlow
from mimi.proof.intents import (
    ALL_INTENTS,
    PROOF_INTENTS,
    ProofForApproval,
    RecordBankWitness,
    RecordPaymentProof,
)
from mimi.proof.ledger import InMemoryProofLedger, ProofRecord, SqlProofLedger
from mimi.proof.reading import ProofReading, ScreenshotReader, fake_reader

__all__ = [
    "ALL_INTENTS",
    "PROOF_HELPER",
    "PROOF_INTENTS",
    "Assessment",
    "ImageWebhook",
    "InMemoryProofLedger",
    "OpenInvoice",
    "ProofDesk",
    "ProofFlow",
    "ProofForApproval",
    "ProofReading",
    "ProofRecord",
    "RecordBankWitness",
    "RecordPaymentProof",
    "ScreenshotReader",
    "SqlProofLedger",
    "assess",
    "fake_reader",
]
