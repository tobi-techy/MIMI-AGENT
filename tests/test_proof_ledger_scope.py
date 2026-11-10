"""T5 (SHOP-RELATIONSHIP-MIGRATION.md §3): the proof ledger is tenant-scoped.

Two tenants share one shop id and one image hash. Every lookup sees only
its own tenant's rows; a foreign proof id reads as missing (KeyError, the
same as a row that is not there); a foreign record is refused on add;
tenant_id cannot be moved by update.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from mimi.proof.ledger import ProofRecord, SqlProofLedger
from mimi.proof.parse import LAGOS

T0 = datetime(2026, 10, 26, 15, 0, tzinfo=LAGOS)
SHA = "d" * 64
TENANTS = ("tenant-a", "tenant-b")


def _record(pid: str, tenant: str, ref: str = "", credit: str = "") -> ProofRecord:
    return ProofRecord(
        proof_id=pid,
        tenant_id=tenant,
        shop_id="shop-005",
        sender="",
        received_at=(T0 - timedelta(hours=2)).isoformat(),
        image_sha256=SHA,
        image_bytes=0,
        reading_json="{}",
        read_via="seed",
        level="likely",
        verdict="likely_genuine",
        reference=ref,
        bank_credit_id=credit,
    )


@pytest.fixture()
def ledgers(tmp_path: Path) -> dict[str, SqlProofLedger]:
    out = {t: SqlProofLedger(sa.create_engine(f"sqlite:///{tmp_path / t}.db"), t)
           for t in TENANTS}
    for ledger in out.values():
        ledger.ensure_schema()
    out["tenant-a"].add(_record("proof-a", "tenant-a", ref="REF-1", credit="cr-a"))
    out["tenant-b"].add(_record("proof-b", "tenant-b", ref="REF-1", credit="cr-b"))
    return out


def test_same_shop_same_image_isolated(ledgers: dict[str, SqlProofLedger]) -> None:
    a, b = ledgers["tenant-a"], ledgers["tenant-b"]
    assert [r.proof_id for r in a.for_shop("shop-005")] == ["proof-a"]
    assert [r.proof_id for r in b.for_shop("shop-005")] == ["proof-b"]
    assert [p.proof_id for p in a.by_image(SHA)] == ["proof-a"]
    assert [p.proof_id for p in b.by_image(SHA)] == ["proof-b"]
    assert [p.proof_id for p in a.by_reference("REF-1")] == ["proof-a"]
    assert [p.proof_id for p in b.by_reference("REF-1")] == ["proof-b"]
    assert a.credit_used("cr-a") and not a.credit_used("cr-b")
    assert b.credit_used("cr-b") and not b.credit_used("cr-a")


def test_get_foreign_proof_id_raises_key_error(
        ledgers: dict[str, SqlProofLedger]) -> None:
    with pytest.raises(KeyError):
        ledgers["tenant-a"].get("proof-b")
    with pytest.raises(KeyError):
        ledgers["tenant-b"].get("proof-a")
    with pytest.raises(KeyError):
        ledgers["tenant-a"].get("no-such-proof")


def test_add_refuses_foreign_record(ledgers: dict[str, SqlProofLedger]) -> None:
    with pytest.raises(ValueError, match="tenant-b.*tenant-a|tenant-a.*tenant-b"):
        ledgers["tenant-a"].add(_record("proof-x", "tenant-b"))
    assert ledgers["tenant-a"].add(_record("proof-x", "tenant-a"))
    assert not ledgers["tenant-a"].add(_record("proof-x", "tenant-a"))


def test_update_cannot_move_tenant(ledgers: dict[str, SqlProofLedger]) -> None:
    with pytest.raises(ValueError, match="tenant"):
        ledgers["tenant-a"].update("proof-a", tenant_id="tenant-b")
    assert ledgers["tenant-a"].get("proof-a").tenant_id == "tenant-a"


def test_update_only_touches_own_row(ledgers: dict[str, SqlProofLedger]) -> None:
    with pytest.raises(KeyError):
        ledgers["tenant-a"].update("proof-b", level="verified")
    assert ledgers["tenant-b"].get("proof-b").level == "likely"


def test_events_are_tenant_scoped(ledgers: dict[str, SqlProofLedger]) -> None:
    from mimi.provenance import ProofEvent

    ev = ProofEvent("payment_transfer", "proof-a", "none", "likely",
                    "alert_screenshot_checked", "proof-a", "v1", T0.isoformat())
    ledgers["tenant-a"].add_event(ev)
    assert ledgers["tenant-a"].events("proof-a") == [ev]
    assert ledgers["tenant-b"].events("proof-a") == []
    with pytest.raises(ValueError, match="tenant"):
        ledgers["tenant-a"].add_event(ev, tenant_id="tenant-b")


def test_add_event_defaults_to_ledger_tenant(tmp_path: Path) -> None:
    from mimi.provenance import ProofEvent

    ledger = SqlProofLedger(sa.create_engine(f"sqlite:///{tmp_path / 'e.db'}"),
                            "tenant-a")
    ledger.ensure_schema()
    ledger.add(_record("proof-a", "tenant-a"))
    ev = ProofEvent("payment_transfer", "proof-a", "none", "likely",
                    "alert_screenshot_checked", "proof-a", "v1", T0.isoformat())
    ledger.add_event(ev)
    assert ledger.events("proof-a") == [ev]
