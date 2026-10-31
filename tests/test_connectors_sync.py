"""Connectors (RAI-277): Sheets/CSV mapping, consent, sync, endpoints."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from mimi.connectors import consent as ct
from mimi.connectors.base import ConnectorError
from mimi.connectors.composio import FakeComposio
from mimi.connectors.csv.connector import CsvConnector
from mimi.connectors.env import ConnectorSettings
from mimi.connectors.mono.connector import FakeMono, MonoStub
from mimi.connectors.sheets.connector import SheetsConnector
from mimi.connectors.sheets.mapping import map_rows, match_sku, suggest_mapping
from mimi.connectors.state import check_state, sign_state
from mimi.connectors.sync import source_fresh, sync
from mimi.store import schema as s
from mimi.store.db import ensure_schema, make_engine


def _engine(tmp_path) -> sa.Engine:
    e = make_engine(f"sqlite:///{tmp_path / 'c.db'}")
    ensure_schema(e)
    return e


def test_mapping_first_suggestion_and_no_invention() -> None:
    assert suggest_mapping(["Item", "Qty left", "Date", "Note"]) == {
        "Item": "item", "Qty left": "qty_left", "Date": "date", "Note": "skip"}
    rows = map_rows([["Item", "Qty left"], ["Noodles", "1,200"], ["Milk", "five"]], {})
    assert rows == [{"item": "Noodles", "qty_left": 1200},
                    {"item": "Milk", "qty_left": None}]
    with pytest.raises(ValueError, match="empty"):
        map_rows([["Item"], ["", ""]], {})
    with pytest.raises(ValueError, match="item column"):
        map_rows([["Price"], ["9"]], {"Price": "skip"})


def test_match_sku_exact_fuzzy_and_miss() -> None:
    catalogue = [{"sku": "SKU-001", "name": "Sunrise Instant Noodles 70g"}]
    exact = match_sku("Sunrise Instant Noodles 70g", catalogue)
    assert exact == {"sku": "SKU-001", "confidence": 1.0, "needs_confirm": False}
    fuzzy = match_sku("Sunrise Instant Noodle 70g", catalogue)
    assert fuzzy["sku"] == "SKU-001" and fuzzy["needs_confirm"] is True
    miss = match_sku("Something entirely else here xyz", catalogue)
    assert miss["sku"] is None and miss["needs_confirm"] is True


def test_csv_bad_files_fail_plain() -> None:
    csvc = CsvConnector(tenant_id="t")
    with pytest.raises(ConnectorError, match="empty"):
        csvc.read("csv", {"text": "", "filename": "x.csv"})
    with pytest.raises(ConnectorError, match="CSV or Excel"):
        csvc.read("csv", {"text": "Item\nNoodles\n", "filename": "x.pdf"})
    ok = csvc.read("csv", {"text": "Item,Qty left\nNoodles,5\n", "filename": "s.csv"})
    assert ok.rows_mapped == 1


def test_sheets_never_writes_and_mono_is_stub() -> None:
    fake = FakeComposio(ConnectorSettings.from_env({}))
    sheets = SheetsConnector(fake, tenant_id="t", connection_id="c")
    with pytest.raises(ConnectorError, match="never writes"):
        sheets.propose_write("anything", {})
    assert MonoStub(tenant_id="t").name == "mono"
    with pytest.raises(ConnectorError, match="not connected yet"):
        MonoStub(tenant_id="t").read("mono")
    vals = FakeMono([{"day": "2026-10-28", "total_kobo": 100}], tenant_id="t")
    assert vals.read("mono").rows_mapped == 1


def test_state_expires_rejects_tamper_and_reuse() -> None:
    good = sign_state("s3cret", {"tenant": "t", "toolkit": "ZOHO_BOOKS"})
    assert check_state("s3cret", good) == {"tenant": "t", "toolkit": "ZOHO_BOOKS"}
    with pytest.raises(ValueError, match="expired"):
        check_state("s3cret", good, now=2**31)
    with pytest.raises(ValueError, match="not valid"):
        check_state("s3cret", good + "x")
    with pytest.raises(ValueError, match="not valid"):
        check_state("other", good)


def test_consent_grant_revoke_and_brand_totals(tmp_path) -> None:
    e = _engine(tmp_path)
    assert not ct.may_read(e, "t", "shop-1", "stock")
    ct.grant(e, "t", "shop-1", "distributor", "stock")
    assert ct.may_read(e, "t", "shop-1", "stock")
    assert not ct.may_read(e, "t", "shop-1", "sales")
    ct.check_read(e, "t", "", "stock")  # the distributor's own book: no grant needed
    with pytest.raises(ConnectorError, match="has not shared"):
        ct.check_read(e, "t", "shop-1", "sales")
    assert ct.revoke(e, "t", "shop-1", "distributor", "stock")
    assert not ct.may_read(e, "t", "shop-1", "stock")
    view = ct.brand_view([{"sku": "A", "quantity": 2, "shop": "S1", "amount_kobo": 9},
                          {"sku": "A", "quantity": 3, "shop": "S2", "amount_kobo": 1}])
    assert view == [{"sku": "A", "total_quantity": 5}]


def test_sync_needs_consent_writes_counts_and_stale(tmp_path) -> None:
    e = _engine(tmp_path)
    fake = FakeComposio(ConnectorSettings.from_env({}))
    sheets = SheetsConnector(fake, tenant_id="t", connection_id="conn-1")
    denied = sync(engine=e, tenant_id="t", connection_id="conn-1",
                  connector=sheets, shop_id="shop-1", today="2026-10-28")
    assert denied["rows_mapped"] == 0 and denied["errors"]
    ct.grant(e, "t", "shop-1", "distributor", "stock")
    run = sync(engine=e, tenant_id="t", connection_id="conn-1",
               connector=sheets, shop_id="shop-1", today="2026-10-28")
    assert run["rows_read"] == 3 and run["rows_mapped"] == 3
    assert source_fresh(e, "t", "shop-1", today="2026-10-28")
    assert not source_fresh(e, "t", "shop-1", today="2026-11-20")
    with e.connect() as conn:
        n = conn.execute(sa.select(sa.func.count()).select_from(
            s.stock_counts).where(s.stock_counts.c.tenant_id == "t")).scalar()
    assert n == 3
