"""Proof levels on the book's records, over the store (RAI-295).

The book's payments and deliveries carry ``proof_level``, ``witnesses`` and
``proof_policy_version`` (alembic ``0010_proof_levels``). This is the one
writer for those columns once a row exists:

* ``add_witness``: a witness landed (a bank credit, a shop YES, a matched
  payment). The level is recomputed by ``mimi.provenance.proof_level`` and an
  append-only ``proof_events`` row is written in the same transaction. A
  replayed witness writes nothing.
* ``ask_once``: True the first time a shop is asked to confirm a record,
  False for ever after. A skipped ask never lowers the level.
* ``level_counts``: records by level per distributor, for the daily count
  and share verified shown to Tobi.

Nothing here moves money; witnesses only ever point at rows that exist.
"""

from __future__ import annotations

import hashlib
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.provenance import (
    POLICY_VERSION,
    PROOF_LEVELS,
    ProofEvent,
    ProofStamp,
    Witness,
    add_witness,
    parse_witnesses,
    payment_type,
)
from mimi.store import schema as s

_TABLES: dict[str, tuple[sa.Table, str]] = {
    "payment": (s.payments, "payment_id"),
    "delivery": (s.deliveries, "delivery_id"),
}


def event_id(event: ProofEvent) -> str:
    key = "|".join((event.record_type, event.record_id, event.witness_kind,
                    event.witness_source, event.policy_version))
    return "pe_" + hashlib.sha256(key.encode()).hexdigest()[:24]


class SqlProofBook:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _row(self, conn: sa.Connection, tenant_id: str, kind: str, record_id: str) -> Any:
        table, key = _TABLES[kind]
        row = conn.execute(sa.select(table).where(table.c.tenant_id == tenant_id,
                                                  table.c[key] == record_id)).first()
        if row is None:
            raise KeyError(f"{kind} {record_id} not in the book")
        return row

    @staticmethod
    def _record_type(kind: str, row: Any) -> str:
        return payment_type(row.method) if kind == "payment" else "delivery"

    def stamp(self, tenant_id: str, kind: str, record_id: str) -> ProofStamp:
        with self.engine.connect() as conn:
            row = self._row(conn, tenant_id, kind, record_id)
        return ProofStamp(self._record_type(kind, row), row.proof_level,  # type: ignore[arg-type]
                          parse_witnesses(row.witnesses), row.proof_policy_version)

    def add_witness(self, tenant_id: str, kind: str, record_id: str, new: Witness,
                    at: str, policy: str = POLICY_VERSION) -> ProofStamp:
        """``kind`` is ``payment`` or ``delivery``. Returns the record's stamp."""
        table, key = _TABLES[kind]
        with self.engine.begin() as conn:
            row = self._row(conn, tenant_id, kind, record_id)
            rtype = self._record_type(kind, row)
            current = ProofStamp(rtype, row.proof_level,  # type: ignore[arg-type]
                                 parse_witnesses(row.witnesses), row.proof_policy_version)
            after, event = add_witness(current, rtype, record_id, new, at, policy)
            if event is None:
                return current
            conn.execute(table.update().where(table.c.tenant_id == tenant_id,
                                              table.c[key] == record_id)
                         .values(**after.columns()))
            conn.execute(s.proof_events.insert().values(
                tenant_id=tenant_id, event_id=event_id(event), **event.as_row()))
        return after

    def events(self, tenant_id: str, record_id: str) -> list[ProofEvent]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(s.proof_events).where(
                s.proof_events.c.tenant_id == tenant_id,
                s.proof_events.c.record_id == record_id)
                .order_by(s.proof_events.c.at, s.proof_events.c.event_id)).mappings().all()
        return [ProofEvent(**{k: v for k, v in dict(r).items()
                              if k not in ("tenant_id", "event_id")}) for r in rows]

    def ask_once(self, tenant_id: str, record_type: str, record_id: str, at: str) -> bool:
        """True the first time; False for ever after (one ask, never chased)."""
        with self.engine.begin() as conn:
            seen = conn.execute(sa.select(s.proof_asks.c.record_id).where(
                s.proof_asks.c.tenant_id == tenant_id,
                s.proof_asks.c.record_type == record_type,
                s.proof_asks.c.record_id == record_id)).first()
            if seen is not None:
                return False
            conn.execute(s.proof_asks.insert().values(
                tenant_id=tenant_id, record_type=record_type, record_id=record_id,
                asked_at=at))
            return True

    def level_counts(self, tenant_id: str) -> dict[str, dict[str, int]]:
        """Records by level, per record table, plus the share verified."""
        out: dict[str, dict[str, int]] = {}
        with self.engine.connect() as conn:
            for kind, (table, _) in _TABLES.items():
                counts: dict[str, int] = {lvl: 0 for lvl in PROOF_LEVELS}
                for lvl, n in conn.execute(
                        sa.select(table.c.proof_level, sa.func.count())
                        .where(table.c.tenant_id == tenant_id)
                        .group_by(table.c.proof_level)):
                    counts[lvl] = counts.get(lvl, 0) + int(n)
                total = sum(counts.values())
                counts["verified_pct"] = (100 * counts["verified"] // total) if total else 0
                out[kind] = counts
        return out
