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

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, replace
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.proof.checks import PriorProof
from mimi.provenance import POLICY_VERSION, ProofEvent

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
    # The proof level the v1 rules give this payment (RAI-295,
    # ``mimi.provenance``), separate from the flow state in ``level``.
    proof_level: str = "estimated"
    witnesses: str = "[]"
    proof_policy_version: str = POLICY_VERSION

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

    def add_event(self, event: ProofEvent) -> None:
        """Append a proof level change (``proof_events``); never edits one."""
        ...

    def events(self, record_id: str) -> list[ProofEvent]: ...


class InMemoryProofLedger:
    def __init__(self) -> None:
        self._rows: dict[str, ProofRecord] = {}
        self._events: list[ProofEvent] = []
        self._lock = threading.Lock()

    def add_event(self, event: ProofEvent) -> None:
        with self._lock:
            self._events.append(event)

    def events(self, record_id: str) -> list[ProofEvent]:
        with self._lock:
            return [e for e in self._events if e.record_id == record_id]

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
    # RAI-295 (alembic 0010_proof_levels).
    sa.Column("proof_level", sa.String(16), nullable=False, server_default="estimated"),
    sa.Column("witnesses", sa.Text(), nullable=False, server_default="[]"),
    sa.Column("proof_policy_version", sa.String(16), nullable=False,
              server_default=POLICY_VERSION),
)
# The same table as ``mimi.store.schema.proof_events`` (alembic 0010), declared
# here so the screenshot ledger can append to it without importing the store.
# Payment screenshots are written under the ledger's tenant.
proof_events = sa.Table(
    "proof_events",
    metadata,
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("event_id", sa.String(64), nullable=False),
    sa.Column("record_type", sa.String(32), nullable=False),
    sa.Column("record_id", sa.String(64), nullable=False),
    sa.Column("old_level", sa.String(16), nullable=False),
    sa.Column("new_level", sa.String(16), nullable=False),
    sa.Column("witness_kind", sa.String(32), nullable=False),
    sa.Column("witness_source", sa.String(200), nullable=False),
    sa.Column("policy_version", sa.String(16), nullable=False),
    sa.Column("at", sa.String(40), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "event_id"),
    sa.Index("ix_proof_events_record", "tenant_id", "record_type", "record_id"),
)


def event_id(event: ProofEvent) -> str:
    """Deterministic, so a replayed witness can never write a second row."""
    key = "|".join((event.record_type, event.record_id, event.witness_kind,
                    event.witness_source, event.policy_version))
    return "pe_" + hashlib.sha256(key.encode()).hexdigest()[:24]


class SqlProofLedger:
    """One tenant's view of the payment proofs.

    Every query ANDs ``tenant_id == self.tenant_id``: a proof id from
    another tenant reads as missing (``get`` raises ``KeyError``), so ids
    cannot be probed for existence across tenants."""

    def __init__(self, engine: Engine, tenant_id: str) -> None:
        self.engine = engine
        self.tenant_id = tenant_id

    def ensure_schema(self) -> None:
        metadata.create_all(self.engine)

    def _tenant(self) -> Any:
        return payment_proofs.c.tenant_id == self.tenant_id

    def add(self, record: ProofRecord) -> bool:
        if record.tenant_id != self.tenant_id:
            raise ValueError(
                f"Proof {record.proof_id} belongs to tenant {record.tenant_id}, "
                f"not to tenant {self.tenant_id}; a ledger never takes another "
                f"tenant's row.")
        with self.engine.begin() as conn:
            seen = conn.execute(
                sa.select(payment_proofs.c.proof_id).where(
                    self._tenant(),
                    payment_proofs.c.proof_id == record.proof_id,
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
                    sa.select(payment_proofs).where(
                        self._tenant(), payment_proofs.c.proof_id == proof_id)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise KeyError(proof_id)
        return ProofRecord(**dict(row))

    def update(self, proof_id: str, **fields: Any) -> ProofRecord:
        if "tenant_id" in fields:
            raise ValueError(
                "A proof's tenant cannot be changed; move the row and you move "
                "another tenant's money trail.")
        with self.engine.begin() as conn:
            conn.execute(
                payment_proofs.update()
                .where(self._tenant(), payment_proofs.c.proof_id == proof_id)
                .values(**fields)
            )
        return self.get(proof_id)

    def _where(self, cond: Any) -> list[ProofRecord]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                sa.select(payment_proofs)
                .where(self._tenant(), cond)
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

    def add_event(self, event: ProofEvent, tenant_id: str = "") -> None:
        eid = event_id(event)
        with self.engine.begin() as conn:
            tid = tenant_id or self.tenant_id
            if tid != self.tenant_id:
                raise ValueError(
                    f"An event for tenant {tid} does not belong on tenant "
                    f"{self.tenant_id}'s ledger.")
            seen = conn.execute(sa.select(proof_events.c.event_id).where(
                proof_events.c.tenant_id == tid, proof_events.c.event_id == eid)).first()
            if seen is None:
                conn.execute(proof_events.insert().values(tenant_id=tid, event_id=eid,
                                                          **event.as_row()))

    def events(self, record_id: str) -> list[ProofEvent]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(proof_events).where(
                proof_events.c.tenant_id == self.tenant_id,
                proof_events.c.record_id == record_id).order_by(proof_events.c.at)).mappings()
            return [ProofEvent(**{k: v for k, v in dict(r).items()
                                  if k not in ("tenant_id", "event_id")}) for r in rows]
