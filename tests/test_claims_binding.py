"""Per-item claims and partial names (RAI-316).

On main at 5f0fe07 claim evidence was per run: one matched bank credit backed
"received" for any invoice, shop or amount, one reminder backed "sent" for any
shop, and a short name ("Mama Sola", "Iya Sola") was never checked. Every bad
case below was sent unchanged there. The controls must still go out.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from mimi.harness import claims as C
from mimi.harness.claims_eval import CASES, judge

NEW = tuple(c for c in CASES if c.cid.startswith(("item-", "partial-")))


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*a: Any, **k: Any) -> None:
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)


def test_the_new_cases_cover_both_gaps() -> None:
    bad = [c for c in NEW if c.bad]
    good = [c for c in NEW if not c.bad]
    assert len(bad) >= 10 and len(good) >= 10
    assert {c.kind for c in bad} == {"claims", "names"}


@pytest.mark.parametrize("case", NEW, ids=[c.cid for c in NEW])
def test_per_item_and_partial_names(case: Any) -> None:
    row = judge(case)
    assert row["ok"], row


def _tool(data: dict[str, Any], call_id: str = "c1", name: str = "check_payment",
          args: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    import json

    return [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args or {})}}]},
        {"role": "tool", "tool_call_id": call_id,
         "content": json.dumps({"ok": True, "data": data})},
    ]


def test_a_credit_backs_only_the_invoice_with_its_amount() -> None:
    msgs = [
        *_tool({"invoices": [
            {"invoice_id": "dlv-00-0055", "shop_id": "shop-005", "outstanding_kobo": 400_000},
            {"invoice_id": "dlv-12-0057", "shop_id": "shop-005", "outstanding_kobo": 410_000}]},
            "c0", "get_invoices", {"shop_id": "shop-005"}),
        *_tool({"shop_id": "shop-005", "matched": True, "credit_id": "cr-1",
                "amount_kobo": 400_000, "label": "verified"}, "c1", "check_payment",
               {"shop_id": "shop-005", "amount_kobo": 400_000}),
    ]
    src = C.sources(msgs)
    ok = C.unbacked_claims("Invoice dlv-00-0055 has been received.", intents=(), src=src,
                           wake_source="shop_message", other_drafts=0)
    bad = C.unbacked_claims("Invoice dlv-12-0057 has been received.", intents=(), src=src,
                            wake_source="shop_message", other_drafts=0)
    assert ok == [] and bad == ["received"]


def test_a_partial_name_maps_to_one_read_name() -> None:
    names = ["Mama Sola Provisions", "Iya Sola Provisions", "Funke & Sons Mini Mart"]
    src = C.Sources(record_text=['{"shop_name": "Mama Sola Provisions"}'])
    assert C.unknown_names(src, "Mama Sola owes money.", names) == []
    assert C.unknown_names(src, "Sola Provisions owes money.", names) == []
    assert C.unknown_names(src, "Iya Sola owes money.", names) == ["Iya Sola"]
    both = C.Sources(record_text=['Mama Sola Provisions', 'Iya Sola Provisions'])
    assert C.unknown_names(both, "Sola Provisions owes money.", names) == ["Sola Provisions"]
    assert C.unknown_names(src, "Two mini marts are late.", names) == []
