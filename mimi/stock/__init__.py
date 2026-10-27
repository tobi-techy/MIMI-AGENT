"""The stock helper (RAI-270): estimator v0.2, one-tap low-stock count, restock draft.

Stock left = exact stock in (deliveries) minus estimated sales, corrected by
the shop's one-tap counts. Code decides every number; Muse Spark only reads
free-text answers into a typed bucket. Mimi never moves money and never
places an order: a restock is a draft in the distributor's approvals.
See ``docs/STOCK.md``.
"""

from mimi.stock.answers import StockAnswer, StockAnswerReader, fake_answer, read_tap
from mimi.stock.estimator import POLICY_VERSION, StockEstimate, estimate
from mimi.stock.flow import STOCK_HELPER, StockDesk, StockFlow, StockLedger
from mimi.stock.history import ItemHistory, StockCount, StockIn, item_histories
from mimi.stock.intents import (
    STOCK_INTENTS,
    LowStockSignal,
    RecordStockCount,
    RestockDraftForApproval,
    SendStockMessage,
)

__all__ = [
    "POLICY_VERSION",
    "STOCK_HELPER",
    "STOCK_INTENTS",
    "ItemHistory",
    "LowStockSignal",
    "RecordStockCount",
    "RestockDraftForApproval",
    "SendStockMessage",
    "StockAnswer",
    "StockAnswerReader",
    "StockCount",
    "StockDesk",
    "StockEstimate",
    "StockFlow",
    "StockIn",
    "StockLedger",
    "estimate",
    "fake_answer",
    "item_histories",
    "read_tap",
]
