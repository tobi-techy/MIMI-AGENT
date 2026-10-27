"""Every payment screenshot Mimi has seen, as fields, never as pixels.

One row per screenshot message: who sent it and when, the image's SHA-256
and size (so a resend or another shop's copy is caught), what Muse Spark
copied off it, the verdict and its reasons, the reference and the invoice it
matched, and, later, the bank credit that witnessed it. The image bytes are
never stored.

In memory for tests and the quick sim; ``SqlProofLedger`` persists the same
(Postgres via alembic ``0005_payment_proofs``, SQLite in tests).
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, replace
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.proof.checks import PriorProof

# "read": read, not yet assessed; "likely": likely paid (screenshot only);
# "review": in the owner's approvals; "ask": Mimi asked for a clearer one;
# "duplicate": the same image again; "verified": the bank's credit witnessed it.
LEVELS = ("read", "likely", "review", "ask", "duplicate", "verified")


@dataclass(frozen=True)
class ProofRecord:
    proof_id: str
    tenant_id: str
    shop_id: str
    sender: str
    received_at: str
    image_sha256: str
    image_bytes: int
    reading_json: str
    read_via: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    level: str = "read"
    verdict: str = ""
    reasons_json: str = "[]"
    amount_kobo: int | None = None
    paid_at: str = ""
    reference: str = ""
    session_id: str = ""
    delivery_ids_json: str = "[]"
    bank_credit_id: str = ""

    @property
    def delivery_ids(self) -> tuple[str, ...]:
        return tuple(json.loads(self.delivery_ids_json))

    def prior(self) -> PriorProof:
        return PriorProof(
            proof_id=self.proof_id,
            shop_id=self.shop_id,
            image_sha256=self.image_sha256,
            reference=self.reference,
            session_id=self.session_id,
            received_at=self.received_at,
            amount_kobo=self.amount_kobo,
            verdict=self.verdict,
            delivery_ids=self.delivery_ids,
        )


class ProofLedger(Protocol):
    def add(self, record: ProofRecord) -> bool:
        """False when the proof id (the message id) was already seen."""
        ...

    def get(self, proof_id: str) -> ProofRecord: ...

    def update(self, proof_id: str, **fields: Any) -> ProofRecord: ...

    def by_image(self, sha256: str) -> list[PriorProof]: ...

    def by_reference(self, ref: str) -> list[PriorProof]: ...

    def for_shop(self, shop_id: str) -> list[ProofRecord]: ...

    def credit_used(self, credit_id: str) -> bool: ...


class InMemoryProofLedger:
    def __init__(self) -> None:
        self._rows: dict[str, ProofRecord] = {}
        self._lock = threading.Lock()

    def add(self, record: ProofRecord) -> bool:
        with self._lock:
            if record.proof_id in self._rows:
                return False
            self._rows[record.proof_id] = record
            return True

    def get(self, proof_id: str) -> ProofRecord:
        with self._lock:
            return self._rows[proof_id]

    def update(self, proof_id: str, **fields: Any) -> ProofRecord:
        with self._lock:
            row = replace(self._rows[proof_id], **fields)
            self._rows[proof_id] = row
            return row

    def _all(self) -> list[ProofRecord]:
        with self._lock:
            return sorted(self._rows.values(), key=lambda r: (r.received_at, r.proof_id))

    def by_image(self, sha256: str) -> list[PriorProof]:
        return [r.prior() for r in self._all() if r.image_sha256 == sha256]

    def by_reference(self, ref: str) -> list[PriorProof]:
        return [r.prior() for r in self._all() if ref and ref in (r.reference, r.session_id)]

    def for_shop(self, shop_id: str) -> list[ProofRecord]:
        return [r for r in self._all() if r.shop_id == shop_id]

    def credit_used(self, credit_id: str) -> bool:
        return any(r.bank_credit_id == credit_id for r in self._all())


metadata = sa.MetaData()
payment_proofs = sa.Table(
    "payment_proofs",
    metadata,
    sa.Column("proof_id", sa.String(), primary_key=True),
    sa.Column("tenant_id", sa.String(), nullable=False),
    sa.Column("shop_id", sa.String(), nullable=False, index=True),
    sa.Column("sender", sa.String(), nullable=False),
    sa.Column("received_at", sa.String(), nullable=False),
    sa.Column("image_sha256", sa.String(), nullable=False, index=True),
    sa.Column("image_bytes", sa.Integer(), nullable=False),
    sa.Column("reading_json", sa.Text(), nullable=False),
    sa.Column("read_via", sa.String(), nullable=False),
    sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("level", sa.String(), nullable=False, server_default="read"),
    sa.Column("verdict", sa.String(), nullable=False, server_default=""),
    sa.Column("reasons_json", sa.Text(), nullable=False, server_default="[]"),
    sa.Column("amount_kobo", sa.BigInteger(), nullable=True),
    sa.Column("paid_at", sa.String(), nullable=False, server_default=""),
    sa.Column("reference", sa.String(), nullable=False, server_default="", index=True),
    sa.Column("session_id", sa.String(), nullable=False, server_default="", index=True),
    sa.Column("delivery_ids_json", sa.Text(), nullable=False, server_default="[]"),
    sa.Column("bank_credit_id", sa.String(), nullable=False, server_default=""),
)


class SqlProofLedger:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def ensure_schema(self) -> None:
        metadata.create_all(self.engine)

    def add(self, record: ProofRecord) -> bool:
        with self.engine.begin() as conn:
            seen = conn.execute(
                sa.select(payment_proofs.c.proof_id).where(
                    payment_proofs.c.proof_id == record.proof_id
                )
            ).first()
            if seen is not None:
                return False
            conn.execute(payment_proofs.insert().values(**asdict(record)))
            return True

    def get(self, proof_id: str) -> ProofRecord:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(payment_proofs).where(payment_proofs.c.proof_id == proof_id)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise KeyError(proof_id)
        return ProofRecord(**dict(row))

    def update(self, proof_id: str, **fields: Any) -> ProofRecord:
        with self.engine.begin() as conn:
            conn.execute(
                payment_proofs.update()
                .where(payment_proofs.c.proof_id == proof_id)
                .values(**fields)
            )
        return self.get(proof_id)

    def _where(self, cond: Any) -> list[ProofRecord]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                sa.select(payment_proofs)
                .where(cond)
                .order_by(payment_proofs.c.received_at, payment_proofs.c.proof_id)
            ).mappings()
            return [ProofRecord(**dict(r)) for r in rows]

    def by_image(self, sha256: str) -> list[PriorProof]:
        return [r.prior() for r in self._where(payment_proofs.c.image_sha256 == sha256)]

    def by_reference(self, ref: str) -> list[PriorProof]:
        if not ref:
            return []
        cond = sa.or_(payment_proofs.c.reference == ref, payment_proofs.c.session_id == ref)
        return [r.prior() for r in self._where(cond)]

    def for_shop(self, shop_id: str) -> list[ProofRecord]:
        return self._where(payment_proofs.c.shop_id == shop_id)

    def credit_used(self, credit_id: str) -> bool:
        return bool(self._where(payment_proofs.c.bank_credit_id == credit_id))
