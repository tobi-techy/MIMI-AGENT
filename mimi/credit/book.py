"""What Credit reads from the system of record, and its stand-in.

Credit needs three reads the Collector's ``BookAPI`` does not have. They are
the contract for Go's credit endpoint (one tenant, consent checked on Go's
side too):

* ``credit_history(tenant, shop)``: every delivery (date, due date, total,
  lines) and every payment (date, amount, method, whether the distributor's
  own bank feed has it, which delivery it paid), promises and disputes;
* ``price_list(tenant)``: the distributor's current unit prices;
* ``open_credit_review(tenant, shop)``: a credit item already waiting in the
  owner's approvals, so a shop asking twice makes one item;
* ``photo_asks(tenant, shop, day)``: shelf-photo asks Go sent the shop that
  Lagos day (the cap on asks reads Go's record, never the model's word).

``CreditGo`` adds them to ``FakeGo`` over the synthetic Coral Lane world.
A fake transfer alert (no money arrived) is in the history with amount 0
and ``bank_confirmed`` False, like ``FakeGo.payments``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from mimi.credit.looks import item_name, pieces_per_unit
from mimi.harness.go import BookAPI, FakeGo


class CreditBook(BookAPI, Protocol):
    def credit_history(self, tenant_id: str, shop_id: str) -> dict[str, Any]: ...

    def price_list(self, tenant_id: str) -> dict[str, tuple[int, int, str, str]]: ...

    def open_credit_review(self, tenant_id: str, shop_id: str) -> str: ...

    def photo_asks(self, tenant_id: str, shop_id: str, day: str) -> int: ...


@dataclass
class CreditGo(FakeGo):
    """``FakeGo`` plus the credit reads. Approvals stay in ``FakeGo.approvals``."""

    credit_reads: int = 0
    _extra: dict[str, Any] = field(default_factory=dict)

    def credit_history(self, tenant_id: str, shop_id: str) -> dict[str, Any]:
        self._own_shop(tenant_id, shop_id)
        self.reads += 1
        self.credit_reads += 1
        w = self.world
        feed = {c.payment_id for c in w.bank_feed}
        due = {r.delivery_id: r for r in w.receivables}
        deliveries = []
        for d in sorted((d for d in w.deliveries if d.shop_id == shop_id), key=lambda d: d.day):
            r = due.get(d.delivery_id)
            deliveries.append({
                "delivery_id": d.delivery_id, "day": d.day, "delivered_on": d.delivered_on,
                "due_day": r.due_day if r else d.day, "due_on": r.due_on if r else d.delivered_on,
                "total_kobo": d.total_kobo,
                "lines": [{"sku": ln.sku, "quantity": ln.quantity} for ln in d.lines],
            })
        payments = []
        for p in sorted((p for p in w.payments if p.shop_id == shop_id),
                        key=lambda p: (p.day, p.payment_id)):
            payments.append({
                "payment_id": p.payment_id, "delivery_id": p.delivery_id, "day": p.day,
                "paid_on": p.paid_on, "amount_kobo": 0 if p.fake_alert else p.amount_kobo,
                "claimed_kobo": p.amount_kobo, "method": p.method,
                "bank_confirmed": p.payment_id in feed, "status": p.status,
            })
        return {
            "live_day": w.live_day,
            "deliveries": deliveries,
            "payments": payments,
            "promises": [{"made_day": p.made_day, "promised_day": p.promised_day,
                          "kept": p.kept} for p in w.promises if p.shop_id == shop_id],
            "open_disputes": sum(1 for d in w.disputes
                                 if d.shop_id == shop_id and d.status == "open"),
        }

    def price_list(self, tenant_id: str) -> dict[str, tuple[int, int, str, str]]:
        self._tenant(tenant_id)
        w = self.world
        return {s.sku: (s.price_on(w.live_day, w.price_rise_day), pieces_per_unit(s),
                        item_name(s), s.unit) for s in w.skus}

    def open_credit_review(self, tenant_id: str, shop_id: str) -> str:
        self._own_shop(tenant_id, shop_id)
        for rec in self.approvals:
            if rec.get("tool") == "submit_credit_review" and rec.get("shop_id") == shop_id:
                return str((rec.get("payload") or {}).get("review_id") or "")
        return ""

    def photo_asks(self, tenant_id: str, shop_id: str, day: str) -> int:
        self._own_shop(tenant_id, shop_id)
        return sum(1 for rec in self.applied
                   if rec.get("tool") == "ask_for_shelf_photo"
                   and rec.get("shop_id") == shop_id
                   and (rec.get("payload") or {}).get("date") == day)
