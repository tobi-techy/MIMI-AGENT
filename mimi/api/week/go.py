"""Go's stand-in for the simulated week: what the distributor's own system does.

Mimi never writes the book. Across a week the book still moves, the way Go
moves it: each morning the book's day rolls forward (yesterday's deliveries
close into receivables), the rep logs new deliveries, the bank feed brings
credits, the rep logs cash. This module is that system, on the same store the
API reads. Every row it writes is synthetic and logged in ``GoLedger``, so
the report can tell Go's changes from anything else (anything else is a money
move by Mimi, and there must be none).

All money is integer kobo. A delivery's total is the sum of its lines,
quantity times the catalogue price of the day (the risen price from the
world's price-rise day on).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.store import schema as s
from mimi.world.model import World

CREDIT_DAYS = 7


@dataclass
class GoLedger:
    """Every book row Go's stand-in wrote during the run."""

    deliveries: list[str] = field(default_factory=list)
    payments: list[str] = field(default_factory=list)
    credits: list[str] = field(default_factory=list)
    receivables: list[str] = field(default_factory=list)


class GoSim:
    def __init__(self, engine: Engine, world: World, tenant: str) -> None:
        self.engine, self.world, self.tenant = engine, world, tenant
        self.ledger = GoLedger()
        self.skus = {k.sku: k for k in world.skus}
        self._n = 0

    # ------------------------------------------------------------- the day
    def open_day(self, day_no: int) -> date:
        """Roll the book to ``day_no``: close the earlier live deliveries into
        receivables and mark overdue what is past its due day."""
        today = self.world.date_of(day_no)
        with self.engine.begin() as conn:
            conn.execute(s.tenants.update().where(s.tenants.c.tenant_id == self.tenant)
                         .values(live_date=today.isoformat(), live_day=day_no))
            old = conn.execute(sa.select(s.deliveries).where(
                s.deliveries.c.tenant_id == self.tenant, s.deliveries.c.live.is_(True),
                s.deliveries.c.day < day_no)).all()
            pos = int(conn.execute(sa.select(sa.func.max(s.receivables.c.position)).where(
                s.receivables.c.tenant_id == self.tenant)).scalar() or 0)
            for d in old:
                have = conn.execute(sa.select(s.receivables.c.delivery_id).where(
                    s.receivables.c.tenant_id == self.tenant,
                    s.receivables.c.delivery_id == d.delivery_id)).first()
                if have is None:
                    paid = int(conn.execute(sa.select(sa.func.sum(s.payments.c.amount_kobo))
                                            .where(s.payments.c.tenant_id == self.tenant,
                                                   s.payments.c.delivery_id == d.delivery_id,
                                                   s.payments.c.fake_alert.is_(False)))
                               .scalar() or 0)
                    pos += 1
                    due = int(d.day) + CREDIT_DAYS
                    out = max(0, int(d.total_kobo) - paid)
                    conn.execute(s.receivables.insert().values(
                        tenant_id=self.tenant, delivery_id=d.delivery_id, shop_id=d.shop_id,
                        due_day=due, due_on=self.world.date_of(due).isoformat(),
                        total_kobo=int(d.total_kobo), paid_kobo=min(paid, int(d.total_kobo)),
                        outstanding_kobo=out, status="settled" if out == 0 else "open",
                        position=pos))
                    self.ledger.receivables.append(d.delivery_id)
                conn.execute(s.deliveries.update().where(
                    s.deliveries.c.tenant_id == self.tenant,
                    s.deliveries.c.delivery_id == d.delivery_id).values(live=False))
            conn.execute(s.receivables.update().where(
                s.receivables.c.tenant_id == self.tenant, s.receivables.c.outstanding_kobo > 0,
                s.receivables.c.due_day < day_no).values(status="overdue"))
        return today

    # ------------------------------------------------------------- deliveries
    def next_delivery_id(self, day_no: int) -> str:
        self._n += 1
        return f"dlv-{day_no}-{7000 + self._n}"

    def add_delivery(self, shop_id: str, day_no: int, minute: int,
                     rng: random.Random, delivery_id: str = "") -> dict[str, Any]:
        """The rep logs a top-up delivery: 2 to 4 of the shop's usual lines."""
        usual: dict[str, Any] = {}
        for d in (*self.world.deliveries, *self.world.live_deliveries):
            if d.shop_id == shop_id:
                for ln in d.lines:
                    usual.setdefault(ln.sku, ln)
        picks = sorted(usual)
        rng.shuffle(picks)
        lines: list[dict[str, Any]] = []
        for sku in sorted(picks[:rng.randint(2, min(4, len(picks)))]):
            k = self.skus[sku]
            price = k.risen_price_kobo if (k.risen_price_kobo is not None
                                           and day_no >= self.world.price_rise_day) \
                else k.price_kobo
            qty = rng.randint(1, 4)
            lines.append({"sku": sku, "name": k.name, "unit": k.unit, "quantity": qty,
                          "unit_price_kobo": int(price), "line_total_kobo": qty * int(price)})
        total = sum(int(ln["line_total_kobo"]) for ln in lines)
        did = delivery_id or self.next_delivery_id(day_no)
        shop = self.world.shop(shop_id)
        with self.engine.begin() as conn:
            conn.execute(s.deliveries.insert().values(
                tenant_id=self.tenant, delivery_id=did, shop_id=shop_id, rep_id=shop.rep_id,
                day=day_no, delivered_on=self.world.date_of(day_no).isoformat(), minute=minute,
                total_kobo=total, status="delivered", witnessed_at=None, live=True))
            conn.execute(s.delivery_lines.insert(), [
                {"tenant_id": self.tenant, "delivery_id": did, "line_no": i, **ln}
                for i, ln in enumerate(lines)])
        self.ledger.deliveries.append(did)
        return {"delivery_id": did, "total_kobo": total, "lines": lines}

    # ------------------------------------------------------------- payments
    def _pay(self, conn: sa.Connection, shop_id: str, amount: int, day_no: int, method: str,
             ref: str) -> str:
        self._n += 1
        pid = f"wk-pay-{self.tenant}-{self._n:04d}"
        # Pay the oldest open receivable first (Go's matching).
        rec = conn.execute(sa.select(s.receivables).where(
            s.receivables.c.tenant_id == self.tenant, s.receivables.c.shop_id == shop_id,
            s.receivables.c.outstanding_kobo > 0).order_by(s.receivables.c.due_day,
                                                           s.receivables.c.delivery_id)).first()
        did = rec.delivery_id if rec is not None else ""
        if not did:
            live = conn.execute(sa.select(s.deliveries.c.delivery_id).where(
                s.deliveries.c.tenant_id == self.tenant, s.deliveries.c.shop_id == shop_id)
                .order_by(s.deliveries.c.day.desc())).first()
            did = live.delivery_id if live is not None else ""
        conn.execute(s.payments.insert().values(
            tenant_id=self.tenant, payment_id=pid, shop_id=shop_id, delivery_id=did,
            day=day_no, paid_on=self.world.date_of(day_no).isoformat(), amount_kobo=amount,
            method=method, reference=ref, status="matched", partial=False, split_of=None,
            fake_alert=False))
        if rec is not None:
            paid = min(int(rec.outstanding_kobo), amount)
            out = int(rec.outstanding_kobo) - paid
            conn.execute(s.receivables.update().where(
                s.receivables.c.tenant_id == self.tenant,
                s.receivables.c.delivery_id == rec.delivery_id).values(
                paid_kobo=int(rec.paid_kobo) + paid, outstanding_kobo=out,
                status="settled" if out == 0 else rec.status))
        self.ledger.payments.append(pid)
        return pid

    def bank_credit(self, shop_id: str, amount: int, day_no: int, received_at: str,
                    narration: str) -> str:
        """A transfer lands in the distributor's account: a payment and its feed credit."""
        with self.engine.begin() as conn:
            pid = self._pay(conn, shop_id, amount, day_no, "transfer", narration[-12:])
            cid = f"wk-feed-{self.tenant}-{self._n:04d}"
            conn.execute(s.bank_feed.insert().values(
                tenant_id=self.tenant, credit_id=cid, payment_id=pid, amount_kobo=amount,
                received_at=received_at, narration=narration))
        self.ledger.credits.append(cid)
        return pid

    def cash_to_rep(self, shop_id: str, amount: int, day_no: int) -> str:
        """The rep logs cash collected at the shop (no bank feed line)."""
        with self.engine.begin() as conn:
            return self._pay(conn, shop_id, amount, day_no, "cash_to_rep", "CASH")


def book_rows(engine: Engine, tenant: str) -> dict[str, Any]:
    """Row ids and credit limits: what the report compares before and after."""
    with engine.connect() as conn:
        return {
            "limits": {r.shop_id: int(r.credit_limit_kobo) for r in conn.execute(
                sa.select(s.shops.c.shop_id, s.shops.c.credit_limit_kobo).where(
                    s.shops.c.tenant_id == tenant))},
            "payments": sorted(r[0] for r in conn.execute(sa.select(s.payments.c.payment_id)
                                                          .where(s.payments.c.tenant_id == tenant))),
            "credits": sorted(r[0] for r in conn.execute(sa.select(s.bank_feed.c.credit_id)
                                                         .where(s.bank_feed.c.tenant_id == tenant))),
            "deliveries": sorted(r[0] for r in conn.execute(
                sa.select(s.deliveries.c.delivery_id).where(s.deliveries.c.tenant_id == tenant))),
        }
