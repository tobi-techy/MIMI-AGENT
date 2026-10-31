"""Stock estimates over the store (RAI-294): the table, the count door, the reads.

``StockBook`` keeps ``stock_estimates`` (append-only) and ``stock_reports``
(what shops told Mimi) next to the book. Every number comes from the book's
confirmed deliveries, the shop's own reports and ``estimate_row``; nothing
here calls a model or moves money.

The interface the weekly check-in (RAI-276) calls::

    book = StockBook(engine)
    result = book.submit_count(
        tenant_id, shop_id, product_id,
        report_id="wamid.ABC",                # the shop's message id: a resend is a no-op
        at=datetime(..., tzinfo=LAGOS),       # when the shop answered, with its zone
        kind="tap", bucket="0-2",             # or kind="typed"|"voice"|"book"|"pos"|
                                              #    "connected_tool", quantity="7", unit="ctn"
        after_delivery=False,                 # answered after that day's delivery?
    )
    result.accepted, result.reason, result.row   # row: the new estimate (or the current one)

    book.report_bought_elsewhere(tenant_id, shop_id, product_id, report_id=..., at=...,
                                 quantity="2", unit="ctn")   # "Bought anything from anyone else?"
    book.count_wanted(tenant_id, shop_id, now=..., lead_time_days=2)
        # the one item worth a one-tap count this week, or None (one ask per shop per week)

Reads for RAI-158, RAI-172, RAI-209, RAI-211: ``latest``, ``history``,
``shop_view`` (the shop's own items, "about" and a confidence word) and
``run_low`` (the distributor's list: run-low flags and confidence words,
never a shop's quantities). Credit never reads this table (RAI-169,
``tests/test_stock_estimates.py``).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.stock.estimator import (
    BUCKET_RANGE,
    CountIn,
    DeliveryIn,
    EstimateRow,
    UnitBook,
    count_wanted,
    estimate_row,
)
from mimi.stock.items import display_name, unit_word
from mimi.store import schema as s
from mimi.store.book import SqlGo
from mimi.tz import lagos, lagos_day

COUNT_KINDS = ("tap", "typed", "voice", "book", "pos", "connected_tool")
CONFIRMED_DELIVERY = ("delivered", "confirmed")
TOP_ITEMS = 3
RUN_LOW_DAYS = 7


@dataclass(frozen=True)
class SubmitResult:
    accepted: bool
    reason: str  # "recorded" | "duplicate" | "unknown_unit" | "bad_count" | "unknown_item"
    row: EstimateRow | None


def _row_from(r: Any) -> EstimateRow:
    d = dict(r._mapping)
    names = {f.name for f in fields(EstimateRow)}
    vals = {k: v for k, v in d.items() if k in names}
    vals["inputs"] = tuple(json.loads(d["inputs"]))
    vals["inputs_hash"] = d["estimate_id"]
    return EstimateRow(**vals)


def _when(days: float | None) -> str:
    if days is None:
        return "not enough history"
    if days < 1:
        return "about now"
    if days < 2:
        return "in about a day"
    if days <= 7:
        return f"in about {round(days)} days"
    return "not this week"


class StockBook:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.go = SqlGo(engine)

    # ------------------------------------------------------------ days

    def _day0(self, tenant_id: str) -> date:
        live_day, _ = self.go.days(tenant_id)
        return self.go.today_for(tenant_id) - timedelta(days=live_day)

    def day_of(self, tenant_id: str, at: datetime) -> int:
        return (lagos_day(at) - self._day0(tenant_id)).days

    # ------------------------------------------------------------ inputs

    def _units(self, tenant_id: str) -> dict[str, tuple[UnitBook, str, int]]:
        live_day, rise = self.go.days(tenant_id)
        out = {}
        for sku in self.go.skus(tenant_id).values():
            cost = sku.price_on(live_day, rise if rise is not None else 10**9)
            out[sku.sku] = (UnitBook.for_sku(sku.sku, sku.name, sku.unit), sku.name, cost)
        return out

    def _inputs(self, tenant_id: str, shop_id: str
                ) -> tuple[dict[str, list[DeliveryIn]], dict[str, list[CountIn]]]:
        ins: dict[str, list[DeliveryIn]] = {}
        for d in self.go.deliveries(tenant_id, shop_id):
            ok = d["status"] in CONFIRMED_DELIVERY
            for ln in d["lines"]:
                ins.setdefault(ln["sku"], []).append(DeliveryIn(
                    f"{d['delivery_id']}:{ln['line_no']}" if "line_no" in ln
                    else f"{d['delivery_id']}:{ln['sku']}", int(d["day"]), int(ln["quantity"]),
                    ln.get("unit", ""), confirmed=ok))
        counts: dict[str, list[CountIn]] = {}
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(s.stock_reports).where(
                s.stock_reports.c.tenant_id == tenant_id,
                s.stock_reports.c.shop_id == shop_id)
                .order_by(s.stock_reports.c.day, s.stock_reports.c.report_id)).all()
        for r in rows:
            if r.kind == "bought_elsewhere":
                ins.setdefault(r.product_id, []).append(DeliveryIn(
                    r.report_id, int(r.day), r.quantity or "0", r.unit, confirmed=True,
                    source="other_supplier"))
            else:
                counts.setdefault(r.product_id, []).append(CountIn(
                    r.report_id, int(r.day), r.kind, bucket=r.bucket, quantity=r.quantity,
                    unit=r.unit, after_delivery=bool(r.after_delivery)))
        return ins, counts

    # ------------------------------------------------------------ writes

    def _write(self, tenant_id: str, row: EstimateRow, now: datetime) -> bool:
        vals = row.as_dict()
        vals.pop("inputs_hash")
        vals["inputs"] = json.dumps(list(row.inputs))
        with self.engine.begin() as conn:
            seen = conn.execute(sa.select(s.stock_estimates.c.estimate_id).where(
                s.stock_estimates.c.tenant_id == tenant_id,
                s.stock_estimates.c.estimate_id == row.inputs_hash)).first()
            if seen is not None:
                return False
            conn.execute(s.stock_estimates.insert().values(
                tenant_id=tenant_id, estimate_id=row.inputs_hash,
                computed_at=lagos(now).isoformat(), **vals))
        return True

    def recompute(self, tenant_id: str, shop_id: str, product_id: str | None = None, *,
                  now: datetime, as_of_day: int | None = None) -> tuple[list[EstimateRow], int]:
        """Estimate every item the shop has (or one). Returns the rows and how
        many were new: the same inputs and policy write nothing."""
        day = self.day_of(tenant_id, now) if as_of_day is None else as_of_day
        units = self._units(tenant_id)
        ins, counts = self._inputs(tenant_id, shop_id)
        visit = self.go.restock_days(tenant_id, shop_id) or None
        day0 = self._day0(tenant_id)
        rows, written = [], 0
        for pid in sorted(set(ins) | set(counts)):
            if product_id is not None and pid != product_id:
                continue
            if pid not in units:
                continue
            ub, name, cost = units[pid]
            row = estimate_row(shop_id, ub, ins.get(pid, []), counts.get(pid, []), day,
                               day0=day0, cost_kobo=cost, visit_days=visit, name=name)
            written += self._write(tenant_id, row, now)
            rows.append(row)
        return rows, written

    def nightly(self, tenant_id: str, *, now: datetime) -> int:
        """Recompute every shop and item; returns new rows written."""
        with self.engine.connect() as conn:
            shops = [r.shop_id for r in conn.execute(sa.select(s.shops.c.shop_id).where(
                s.shops.c.tenant_id == tenant_id).order_by(s.shops.c.shop_id))]
        return sum(self.recompute(tenant_id, sh, now=now)[1] for sh in shops)

    def _report(self, tenant_id: str, shop_id: str, product_id: str, *, report_id: str,
                at: datetime, kind: str, bucket: str | None, quantity: str | None, unit: str,
                after_delivery: bool) -> SubmitResult:
        units = self._units(tenant_id)
        self.go.shop(tenant_id, shop_id)  # the shop is the tenant's (raises otherwise)
        if product_id not in units:
            return SubmitResult(False, "unknown_item", None)
        at = lagos(at)  # refuses a time with no zone
        ub = units[product_id][0]
        if kind == "tap":
            if bucket not in BUCKET_RANGE:
                return SubmitResult(False, "bad_count", None)
        elif quantity is None:
            return SubmitResult(False, "bad_count", None)
        elif ub.to_base(quantity, unit) is None:
            return SubmitResult(False, "unknown_unit", None)
        day = self.day_of(tenant_id, at)
        with self.engine.begin() as conn:
            seen = conn.execute(sa.select(s.stock_reports.c.report_id).where(
                s.stock_reports.c.tenant_id == tenant_id,
                s.stock_reports.c.report_id == report_id)).first()
            if seen is None:
                conn.execute(s.stock_reports.insert().values(
                    tenant_id=tenant_id, report_id=report_id, shop_id=shop_id,
                    product_id=product_id, kind=kind, day=day, at=at.isoformat(),
                    bucket=bucket, quantity=None if quantity is None else str(quantity),
                    unit=unit, after_delivery=after_delivery))
        rows, _ = self.recompute(tenant_id, shop_id, product_id, now=at, as_of_day=day)
        return SubmitResult(seen is None, "recorded" if seen is None else "duplicate",
                            rows[0] if rows else None)

    def submit_count(self, tenant_id: str, shop_id: str, product_id: str, *, report_id: str,
                     at: datetime, kind: str, bucket: str | None = None,
                     quantity: str | int | None = None, unit: str = "",
                     after_delivery: bool = False) -> SubmitResult:
        """A count of any kind (the RAI-276 door). It always overrides the
        estimate; a resend of the same ``report_id`` changes nothing."""
        if kind not in COUNT_KINDS:
            return SubmitResult(False, "bad_count", None)
        return self._report(tenant_id, shop_id, product_id, report_id=report_id, at=at,
                            kind=kind, bucket=bucket,
                            quantity=None if quantity is None else str(quantity), unit=unit,
                            after_delivery=after_delivery)

    def report_bought_elsewhere(self, tenant_id: str, shop_id: str, product_id: str, *,
                                report_id: str, at: datetime, quantity: str | int,
                                unit: str) -> SubmitResult:
        """The weekly "Bought anything from anyone else?" answer: exact stock in."""
        return self._report(tenant_id, shop_id, product_id, report_id=report_id, at=at,
                            kind="bought_elsewhere", bucket=None, quantity=str(quantity),
                            unit=unit, after_delivery=False)

    # ------------------------------------------------------------ reads

    def history(self, tenant_id: str, shop_id: str, product_id: str) -> list[EstimateRow]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(s.stock_estimates).where(
                s.stock_estimates.c.tenant_id == tenant_id,
                s.stock_estimates.c.shop_id == shop_id,
                s.stock_estimates.c.product_id == product_id)
                .order_by(s.stock_estimates.c.as_of_day, s.stock_estimates.c.computed_at,
                          s.stock_estimates.c.estimate_id)).all()
        return [_row_from(r) for r in rows]

    def _latest_all(self, tenant_id: str, shop_id: str | None = None) -> list[EstimateRow]:
        q = sa.select(s.stock_estimates).where(s.stock_estimates.c.tenant_id == tenant_id)
        if shop_id is not None:
            q = q.where(s.stock_estimates.c.shop_id == shop_id)
        with self.engine.connect() as conn:
            rows = conn.execute(q.order_by(s.stock_estimates.c.as_of_day,
                                           s.stock_estimates.c.computed_at,
                                           s.stock_estimates.c.estimate_id)).all()
        latest: dict[tuple[str, str], EstimateRow] = {}
        for r in rows:
            latest[(r.shop_id, r.product_id)] = _row_from(r)
        return [latest[k] for k in sorted(latest)]

    def latest(self, tenant_id: str, shop_id: str, product_id: str) -> EstimateRow | None:
        rows = self.history(tenant_id, shop_id, product_id)
        return rows[-1] if rows else None

    def shop_view(self, tenant_id: str, shop_id: str) -> list[dict[str, Any]]:
        """The shop's own items: about how many are left, when they may run
        out, and how sure Mimi is, in words. Always "about": an estimate."""
        names = {pid: n for pid, (_, n, _) in self._units(tenant_id).items()}
        out = []
        for r in self._latest_all(tenant_id, shop_id):
            item = display_name(names.get(r.product_id, r.product_id))
            if r.status != "ok" or r.est_left is None:
                text = f"{item}: not enough history yet."
            else:
                n = r.est_left
                qty = f"{n:g}" if n == int(n) else f"{n:.1f}"
                text = (f"{item}: about {qty} {unit_word(r.unit, 1 if n == 1 else 2)} left, "
                        f"may run out {_when(r.days_to_run_out)} "
                        f"({r.confidence_word.lower()} confidence, estimated).")
            out.append({"product_id": r.product_id, "item": item, "status": r.status,
                        "est_left": r.est_left, "unit": r.unit,
                        "run_out_date": r.run_out_date, "confidence": r.confidence_word,
                        "proof_level": r.proof_level, "money_tied_kobo": r.money_tied_kobo,
                        "text": text})
        return out

    def run_low(self, tenant_id: str, *, within_days: int = RUN_LOW_DAYS
                ) -> list[dict[str, Any]]:
        """The distributor's "likely to run low" list: per item, the shops
        likely to run out within ``within_days``, with a confidence word.
        Never a shop's quantities (RAI-294 guardrail)."""
        names = {pid: n for pid, (_, n, _) in self._units(tenant_id).items()}
        with self.engine.connect() as conn:
            shop_names = {r.shop_id: r.name for r in conn.execute(
                sa.select(s.shops.c.shop_id, s.shops.c.name).where(
                    s.shops.c.tenant_id == tenant_id))}
        per: dict[str, list[dict[str, str]]] = {}
        for r in self._latest_all(tenant_id):
            if r.status != "ok" or r.days_to_run_out is None or r.days_to_run_out > within_days:
                continue
            per.setdefault(r.product_id, []).append({
                "shop": shop_names.get(r.shop_id, ""), "shop_id": r.shop_id,
                "likely_low": "yes", "when": _when(r.days_to_run_out),
                "confidence": r.confidence_word})
        out = []
        for pid in sorted(per, key=lambda p: (-len(per[p]), p)):
            item = display_name(names.get(pid, pid))
            shops = sorted(per[pid], key=lambda x: x["shop"])
            n = len(shops)
            out.append({"product_id": pid, "item": item, "shops_likely_low": n,
                        "text": f"{n} shop{'s' if n != 1 else ''} likely to run low on {item} "
                                "this week (estimated).",
                        "shops": shops})
        return out

    def count_wanted(self, tenant_id: str, shop_id: str, *, now: datetime,
                     lead_time_days: int, rows: Sequence[EstimateRow] | None = None
                     ) -> str | None:
        """The one item to ask the shop to count this week (RAI-276), or None.
        At most one such ask per shop per week, never repeated if skipped."""
        rows = list(rows) if rows is not None else self._latest_all(tenant_id, shop_id)
        recent = sorted(rows, key=lambda r: -(r.est_daily_rate or 0))
        top = {r.product_id for r in recent[:TOP_ITEMS]}
        hits = [r for r in rows if count_wanted(r, lead_time_days=lead_time_days,
                                                top_item=r.product_id in top)]
        if not hits:
            return None
        pick = min(hits, key=lambda r: (r.days_to_run_out if r.days_to_run_out is not None
                                        else 99, r.confidence, r.product_id))
        day = lagos_day(lagos(now))
        monday = day - timedelta(days=day.weekday())
        if not self.go.ask_owner_once(tenant_id, f"stock-count:{shop_id}", monday):
            return None
        return pick.product_id
