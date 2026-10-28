"""RAI-306: the Order skills' book slices carry no money.

The distributor sets credit and code checks it (``orders.order.credit_view``),
so the Order helper never sees a credit limit, a balance or an invoice amount.
Order intake gets a code-made ``credit_room`` band; drop confirmation gets the
open deliveries' ids and due dates only; ``OrderOut`` has no available credit.
These tests failed on main (42b77e0).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mimi.world import load_world

W = load_world()
MONEY = ("credit_limit", "outstanding", "overdue", "_kobo", "_naira", "amount", "balance")


def _slice(skill: str, shop_id: str) -> dict[str, Any]:
    from mimi.harness.context import ContextBuilder, Manifest
    from mimi.harness.context_eval import _at
    from mimi.harness.go import FakeGo
    from mimi.harness.scenarios import WAT
    from mimi.harness.types import RunEvent

    b = ContextBuilder(book=FakeGo(W))
    ev = RunEvent(event_id="e", tenant_id=W.tenant_id, thread_id="t",
                  wake_source="shop_message", text="hi", sender="shop",
                  shop_id=shop_id, at=_at(W, WAT))
    man = Manifest(tenant_id=W.tenant_id, thread_id="t", helper="order", skill=skill,
                   budget_tokens=1)
    data, _ = b._slice(ev, skill, shop_id, man)
    return data


def _keys(obj: Any) -> list[str]:
    if isinstance(obj, dict):
        return [str(k) for k in obj] + [k for v in obj.values() for k in _keys(v)]
    if isinstance(obj, list):
        return [k for v in obj for k in _keys(v)]
    return []


def _shops_with_open_invoices() -> list[str]:
    from mimi.harness.go import FakeGo

    go = FakeGo(W)
    return [s.shop_id for s in W.shops
            if s.credit_limit_kobo > 0 and go.receivables(W.tenant_id, s.shop_id)]


def test_poc_order_slices_hold_no_money_figures() -> None:
    shops = _shops_with_open_invoices()
    assert shops
    for skill in ("order/order-intake", "order/drop-confirmation"):
        for shop_id in shops:
            data = _slice(skill, shop_id)
            assert data, (skill, shop_id)
            bad = [k for k in _keys(data) if any(m in k for m in MONEY)]
            assert not bad, (skill, shop_id, bad, json.dumps(data)[:300])


def test_order_intake_slice_is_a_credit_band() -> None:
    for shop_id in _shops_with_open_invoices():
        data = _slice("order/order-intake", shop_id)
        assert set(data) == {"shop_id", "shop_name", "credit_room"}, data
        assert data["credit_room"] in ("ok", "low", "none")


def test_drop_confirmation_slice_lists_ids_and_due_dates_only() -> None:
    for shop_id in _shops_with_open_invoices():
        data = _slice("order/drop-confirmation", shop_id)
        assert data["open_invoices"]
        for row in data["open_invoices"]:
            assert set(row) == {"invoice_id", "due_on"}, row


@pytest.mark.parametrize(("limit", "open_", "band"), [
    (0, 0, "none"),                     # no credit line
    (0, 5_000_00, "none"),
    (50_000_000, 60_000_000, "none"),   # over its limit
    (50_000_000, 50_000_000, "none"),   # nothing left
    (50_000_000, 41_000_000, "low"),    # 18% left
    (50_000_000, 40_000_000, "ok"),     # exactly 20% left
    (50_000_000, 0, "ok"),
])
def test_credit_room_band(limit: int, open_: int, band: str) -> None:
    from mimi.harness.context import credit_room

    assert credit_room(limit, open_) == band


def test_order_out_has_no_available_credit() -> None:
    from mimi.orders.tools import OrderOut

    assert "available_credit_kobo" not in OrderOut.model_fields
    assert {"over_credit", "over_by_kobo", "extends_credit"} <= set(OrderOut.model_fields)
