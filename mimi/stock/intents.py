"""Typed proposals for the stock helper. None moves money or places an order.

* ``LowStockSignal``: what the distributor sees, "likely to run low" with a
  confidence and a band of days, never the shop's counts or exact units.
* ``SendStockMessage``: a WhatsApp message to the shop (the one-tap question
  with its three buttons, the one follow-up, the thanks).
* ``RecordStockCount``: the shop's own answer, kept by Mimi for the estimate
  (``visibility`` is fixed to ``mimi_only``: not shown to the distributor).
* ``RestockDraftForApproval``: a restock draft in the distributor's
  approvals. ``status`` is fixed to ``draft`` and ``places_order`` to False:
  nothing is ordered until the distributor approves, and the distributor
  decides any credit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from mimi.runtime.outbox import intent_types
from mimi.stock.estimator import Confidence
from mimi.types import ShopScope

StockPurpose = Literal["stock_ask", "stock_ask_again", "stock_thanks_draft", "stock_thanks_noted"]
RunOutBand = Literal["under_3_days", "3_to_7_days", "over_7_days"]


def band_for(days: float | None) -> RunOutBand:
    if days is None or days >= 7:
        return "over_7_days"
    return "under_3_days" if days < 3 else "3_to_7_days"


@dataclass(frozen=True)
class LowStockSignal:
    scope: ShopScope
    sku: str
    item: str
    run_out_band: RunOutBand
    confidence: Confidence
    as_of: str  # ISO date, Africa/Lagos
    policy_version: str
    kind: Literal["low_stock_signal"] = field(default="low_stock_signal")


@dataclass(frozen=True)
class SendStockMessage:
    scope: ShopScope
    sku: str
    purpose: StockPurpose
    text: str
    # WhatsApp reply buttons for the one-tap question (at most 3).
    buttons: tuple[str, ...] = ()
    # The restock quantity named in a thanks, from code (0 when none).
    draft_quantity: int = 0
    kind: Literal["send_stock_message"] = field(default="send_stock_message")


@dataclass(frozen=True)
class RecordStockCount:
    scope: ShopScope
    sku: str
    bucket: Literal["0-2", "3-5", "6+"]
    # The units the estimate now assumes: the exact number when the shop gave
    # one, else the bucket's middle (``history.assumed_units``).
    units_assumed: int
    exact: bool
    answer_message_id: str
    answer_text: str
    answer_via: str  # tap, typed, model
    counted_on: str  # ISO date, Africa/Lagos
    visibility: Literal["mimi_only"] = "mimi_only"
    kind: Literal["record_stock_count"] = field(default="record_stock_count")


@dataclass(frozen=True)
class RestockDraftForApproval:
    scope: ShopScope
    draft_id: str
    sku: str
    item: str
    unit: str
    quantity: int
    unit_price_kobo: int
    amount_kobo: int
    run_out_band: RunOutBand
    confidence: Confidence
    reason: str  # "shop_count" (the shop said how many are left)
    policy_version: str
    status: Literal["draft"] = "draft"
    needs: Literal["distributor_approval"] = "distributor_approval"
    places_order: Literal[False] = False
    kind: Literal["restock_draft_for_approval"] = field(default="restock_draft_for_approval")


STOCK_INTENTS: tuple[type, ...] = intent_types(
    LowStockSignal, SendStockMessage, RecordStockCount, RestockDraftForApproval
)
