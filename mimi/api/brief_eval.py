"""The daily brief eval (MIAG-10): thirteen hand-marked scenarios over real imports.

    python -m mimi.api.brief_eval             # every scenario (offline, no model)
    python -m mimi.api.brief_eval --only cap  # one scenario by its id prefix

Each scenario is a synthetic distributor book written as CSV, imported through
the real importer (``mimi.connectors.book``), then read through the real routine
(``routines.run_now`` at 8:00 Africa/Lagos), so what is marked is the message an
owner would receive, not a function called in a test.

The findings are worked out by hand from the rows: which shop must be flagged,
which must come first, how old the debt is, what the returns section must say.
A mark fails when the brief is *wrong*, not only when it is missing, and every
scenario also carries the traceability mark: every naira figure in the message
must be a row's amount or a sum of rows (the numbers are read from the database
here, not from the brief), and every plain number must be a day, a quantity, a
count or one of the fixed bands the brief names. That is the mechanical version
of "no number is typed into a template".

No model is called anywhere in this path, so nothing here is recorded or
replayed.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from mimi.api import brief as brief_mod
from mimi.api import routines as rt
from mimi.credit.exposure import bucket_of
from mimi.evalmarks import exit_code, slice_rate
from mimi.store import ensure_schema, make_engine
from mimi.store import schema as s
from mimi.types import RETURN_OPEN_STATUSES
from mimi.tz import LAGOS

TENANT = "eval-dist"
#: Every fixture line is one carton at this price, so a delivery total is a
#: quantity times a price and the hand arithmetic stays visible.
UNIT_NAIRA = 1000
#: The fixed bands and windows the brief's words carry.
FIXED_NUMBERS = {30, 31, 60, brief_mod.BEAT_DAYS, brief_mod.ESCALATE_AFTER_DAYS,
                 brief_mod.ITEM_CAP}

NAIRA_RE = re.compile(r"\u20a6([\d,]+(?:\.\d\d)?)")


# ------------------------------------------------------------------ the book


@dataclass(frozen=True)
class Return:
    return_id: str
    raised_on: str
    lines: tuple[tuple[str, int], ...]
    status: str = "drafted"
    reason: str = "damaged"
    delivery_id: str = ""


@dataclass
class Store:
    """One shop in a scenario, with its deliveries, invoices, cash and returns."""

    name: str
    rep: str = "rep-1"
    limit_naira: int = 0
    restock: int = 7
    area: str = "Ikeja"
    #: (delivered_on, total_naira); each delivery is one line of cartons.
    deliveries: list[tuple[str, int]] = field(default_factory=list)
    #: (due_on, total_naira, paid_naira), aligned to ``deliveries`` by position.
    invoices: list[tuple[str, int, int]] = field(default_factory=list)
    #: (paid_on, amount_naira, method)
    payments: list[tuple[str, int, str]] = field(default_factory=list)
    returns: list[Return] = field(default_factory=list)

    @property
    def shop_id(self) -> str:
        return "shop-" + self.name.lower().replace(" ", "-")


@dataclass
class Scenario:
    id: str
    stores: list[Store]
    marks: tuple[tuple[str, Callable[[Context], bool]], ...]
    #: One entry per import-and-run; a later entry sees the earlier snapshots, so
    #: "what changed" is exercised by giving it two days, each with the book as
    #: it stood that morning.
    runs: list[tuple[str, list[Store]]] = field(default_factory=list)
    #: Run the same book twice in two fresh databases (the determinism mark).
    repeat: bool = False

    def timeline(self) -> list[tuple[str, list[Store]]]:
        return self.runs or [(book_day(self.stores), self.stores)]


@dataclass
class Context:
    text: str
    brief: brief_mod.Brief
    engine: Any
    book_day: str
    texts: list[str] = field(default_factory=list)
    repeat_text: str = ""


def book_day(stores: Sequence[Store]) -> str:
    days = [d for one in stores for d, _total in one.deliveries]
    days += [paid for one in stores for paid, _amount, _method in one.payments]
    return max(days)


#: Two reps and two products, so every item can name a person and a product.
REPS = (("rep-1", "Abiola Nwosu"), ("rep-2", "Chinelo Eze"))
SKUS = (("SKU-A", "Golden Soya Oil 5L"), ("SKU-B", "Sunrise Noodles 70g"))


def _tables(stores: Sequence[Store]) -> dict[str, list[list[str]]]:
    rows: dict[str, list[list[str]]] = {
        "distributor": [["tenant_id", "name", "owner_phones"],
                        [TENANT, "Eval Distributors", "+2348000000100"]],
        "reps": [["rep_id", "name", "phone", "areas"]]
        + [[rep_id, name, f"+23480000002{index:02d}", "Ikeja|Agege"]
           for index, (rep_id, name) in enumerate(REPS)],
        "shops": [["shop_id", "name", "owner", "area", "street", "phone", "rep_id",
                   "credit_limit_naira", "restock_days"]],
        "skus": [["sku", "name", "brand", "unit", "price_naira"]]
        + [[sku, name, "Eval", "carton", f"{UNIT_NAIRA}.00"] for sku, name in SKUS],
        "deliveries": [["delivery_id", "shop_id", "rep_id", "delivered_on", "minute"]],
        "delivery_lines": [["delivery_id", "sku", "quantity", "unit_price_naira"]],
        "receivables": [["delivery_id", "shop_id", "due_on", "total_naira", "paid_naira"]],
        "payments": [["payment_id", "shop_id", "delivery_id", "paid_on", "amount_naira",
                      "method", "reference"]],
        "returns": [["return_id", "shop_id", "rep_id", "raised_on", "status", "reason",
                     "delivery_id"]],
        "return_lines": [["return_id", "line_no", "sku", "quantity", "unit_price_naira"]],
    }
    for shop in stores:
        rows["shops"].append([shop.shop_id, shop.name, "Owner " + shop.name, shop.area,
                              "1 Eval Road", f"+23480000003{len(rows['shops']):02d}",
                              shop.rep, f"{shop.limit_naira}.00", str(shop.restock)])
        for index, (delivered_on, total) in enumerate(shop.deliveries, start=1):
            delivery_id = f"{shop.shop_id}-d{index}"
            rows["deliveries"].append([delivery_id, shop.shop_id, shop.rep, delivered_on,
                                       "600"])
            rows["delivery_lines"].append([delivery_id, "SKU-A", "1", f"{total}.00"])
        for index, (due_on, total, paid) in enumerate(shop.invoices, start=1):
            rows["receivables"].append([f"{shop.shop_id}-d{index}", shop.shop_id, due_on,
                                        f"{total}.00", f"{paid}.00"])
        for index, (paid_on, amount, method) in enumerate(shop.payments, start=1):
            rows["payments"].append([f"{shop.shop_id}-p{index}", shop.shop_id, "", paid_on,
                                     f"{amount}.00", method, ""])
        for one in shop.returns:
            rows["returns"].append([one.return_id, shop.shop_id, shop.rep, one.raised_on,
                                    one.status, one.reason, one.delivery_id])
            for line_no, (sku, quantity) in enumerate(one.lines, start=1):
                rows["return_lines"].append([one.return_id, str(line_no), sku,
                                             str(quantity), f"{UNIT_NAIRA}.00"])
    return rows


def _write(tables: dict[str, list[list[str]]], folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for table, grid in tables.items():
        with (folder / f"{table}.csv").open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(grid)
    return folder


def _run_scenario(sc: Scenario, workdir: Path, label: str = "") -> Context:
    from mimi.connectors.book import import_book

    engine = make_engine(f"sqlite:///{workdir / (sc.id + label + '.db')}")
    ensure_schema(engine)
    texts: list[str] = []
    ctx = Context(text="", brief=brief_mod.Brief(day=date.today(), book_day=date.today()),
                  engine=engine, book_day="")
    for index, (day, stores) in enumerate(sc.timeline(), start=1):
        book = _write(_tables(stores), workdir / f"{sc.id}{label}-{index}")
        import_book(engine, book, tenant_id=TENANT)
        moment = datetime.fromisoformat(day).replace(hour=8, tzinfo=LAGOS)
        rt.run_now(engine, TENANT, "morning_brief", now=moment)
        with engine.connect() as conn:
            text = conn.execute(sa.select(s.messages.c.text).where(
                s.messages.c.tenant_id == TENANT,
                s.messages.c.kind == "morning_brief")).scalars().all()[-1]
        texts.append(str(text))
    ctx.texts = texts
    ctx.text = texts[-1]
    ctx.book_day = sc.timeline()[-1][0]
    ctx.brief = brief_mod.build(engine, TENANT, date.fromisoformat(ctx.book_day))
    engine.dispose()
    return ctx


# --------------------------------------------------------------- text helpers


def section(text: str, head: str) -> str:
    """The lines from ``head`` up to the next section head (or the end)."""
    heads = ("Mimi morning brief", "What changed", "Money at risk", "Collections today",
             "Returns:", "Coverage:", "The book has no delivery", "items listed")
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(head)), None)
    if start is None:
        return ""
    out = [lines[start]]
    for line in lines[start + 1:]:
        if any(line.startswith(other) for other in heads):
            break
        out.append(line)
    return "\n".join(out)


def line_for(text: str, name: str, head: str = "") -> str:
    """The numbered item line (or section line) that names ``name``."""
    body = section(text, head) if head else text
    return next((line for line in body.splitlines() if name in line), "")


def item_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if re.match(r"^\d+\. ", line)]


def has(text: str, *needles: str) -> bool:
    return all(needle in text for needle in needles)


# ------------------------------------------------------- the traceability mark


def _allowed_numbers(engine: Any, book_day: str) -> tuple[set[int], set[int]]:
    """(every kobo figure a row holds or a row sum gives, every plain number).

    Read from the database here, never from the brief, so a figure the brief
    invented cannot pass by describing itself.
    """
    kobo: set[int] = set()
    ints: set[int] = set(FIXED_NUMBERS) | set(range(0, 21))
    day = date.fromisoformat(book_day)
    ints.update({day.day, day.year})
    with engine.connect() as conn:
        invoices = conn.execute(sa.select(
            s.receivables.c.shop_id, s.receivables.c.total_kobo, s.receivables.c.paid_kobo,
            s.receivables.c.outstanding_kobo, s.receivables.c.due_day).where(
                s.receivables.c.tenant_id == TENANT)).all()
        for r in invoices:
            kobo.update({int(r.total_kobo), int(r.paid_kobo), int(r.outstanding_kobo)})
            days = max(0, day.toordinal() - int(r.due_day or 0))
            ints.add(days)
        payments = conn.execute(sa.select(s.payments.c.paid_on, s.payments.c.amount_kobo)
                                .where(s.payments.c.tenant_id == TENANT)).all()
        for r in payments:
            kobo.add(int(r.amount_kobo))
        deliveries = conn.execute(sa.select(
            s.deliveries.c.shop_id, s.deliveries.c.total_kobo, s.deliveries.c.day).where(
                s.deliveries.c.tenant_id == TENANT)).all()
        last_delivery: dict[str, int] = {}
        for r in deliveries:
            kobo.add(int(r.total_kobo))
            ints.add(max(0, day.toordinal() - int(r.day or 0)))
            last_delivery[str(r.shop_id)] = max(last_delivery.get(str(r.shop_id), 0),
                                                int(r.day or 0))
        restock: dict[str, int] = {}
        for r in conn.execute(sa.select(s.shops.c.shop_id, s.shops.c.credit_limit_kobo,
                                        s.shops.c.restock_days).where(
                s.shops.c.tenant_id == TENANT)):
            kobo.add(int(r.credit_limit_kobo))
            ints.add(int(r.restock_days))
            restock[str(r.shop_id)] = int(r.restock_days or 0)
        returns = conn.execute(sa.select(s.returns.c.return_id, s.returns.c.status,
                                         s.returns.c.raised_day,
                                         s.returns.c.value_drafted_kobo,
                                         s.returns.c.value_collected_kobo,
                                         s.returns.c.value_returned_kobo).where(
                s.returns.c.tenant_id == TENANT)).all()
        open_ids = set()
        for r in returns:
            kobo.update({int(r.value_drafted_kobo), int(r.value_collected_kobo or 0),
                         int(r.value_returned_kobo or 0)})
            ints.add(max(0, day.toordinal() - int(r.raised_day or 0)))
            if str(r.status) in RETURN_OPEN_STATUSES:
                open_ids.add(str(r.return_id))
        kobo.add(sum(int(r.value_drafted_kobo) for r in returns
                     if str(r.return_id) in open_ids))
        by_sku: dict[str, int] = {}
        for r in conn.execute(sa.select(
                s.return_lines.c.return_id, s.return_lines.c.sku,
                s.return_lines.c.value_drafted_kobo, s.return_lines.c.quantity_drafted).where(
                s.return_lines.c.tenant_id == TENANT)).all():
            kobo.add(int(r.value_drafted_kobo))
            ints.add(int(r.quantity_drafted))
            if str(r.return_id) in open_ids:
                by_sku[str(r.sku)] = by_sku.get(str(r.sku), 0) + int(r.value_drafted_kobo)
        kobo.update(by_sku.values())
        shops = conn.execute(sa.select(s.shops.c.shop_id).where(
            s.shops.c.tenant_id == TENANT)).scalars().all()
        ints.add(len(shops))
        # Per-shop sums, the book's totals, the ageing bands, and what a payment
        # window adds up to: the sums the brief actually prints.
        per_shop: dict[str, dict[str, int]] = {}
        for r in invoices:
            one = per_shop.setdefault(str(r.shop_id), {"out": 0, "over": 0})
            one["out"] += int(r.outstanding_kobo)
            if day.toordinal() - int(r.due_day or 0) > 0:
                one["over"] += int(r.outstanding_kobo)
        kobo.update({value for one in per_shop.values() for value in one.values()})
        kobo.add(sum(one["out"] for one in per_shop.values()))
        kobo.add(sum(one["over"] for one in per_shop.values()))
        bands: dict[str, int] = {}
        for r in invoices:
            days = max(0, day.toordinal() - int(r.due_day or 0))
            key = bucket_of(days)
            bands[key] = bands.get(key, 0) + int(r.outstanding_kobo)
        for value in bands.values():
            kobo.add(value)
        days_of_cash = sorted({str(r.paid_on) for r in payments})
        for since in days_of_cash:
            kobo.add(sum(int(r.amount_kobo) for r in payments if str(r.paid_on) > since))
        kobo.add(sum(int(r.amount_kobo) for r in payments))
        # The shop's values by date, so a running balance (a movement) traces too.
        totals = [sum(one["out"] for one in per_shop.values()),
                  sum(one["over"] for one in per_shop.values()),
                  sum(int(r.amount_kobo) for r in payments)]
        kobo.update(abs(a - b) for a in totals for b in totals)
        # How many items the book holds, counted from the rows: debtors, overdue
        # shops, open returns, the shops past their rhythm, and the one product
        # flag when a SKU came back from three shops inside a beat. The brief's
        # "the rest counted" line is checked against this count, not its own.
        measured = _measured_shops(last_delivery, restock, day)
        repeat = any(len(shops) >= 3 for shops in _sku_shops(conn, day).values())
        candidates = (len(per_shop) + len(_overdue_shops(invoices, day)) + len(open_ids)
                      + measured + (1 if repeat else 0))
        ints.update({candidates, max(0, candidates - brief_mod.ITEM_CAP),
                     min(brief_mod.ITEM_CAP, candidates)})
    return kobo, ints


def _overdue_shops(invoices: Sequence[Any], day: date) -> set[str]:
    return {str(r.shop_id) for r in invoices
            if max(0, day.toordinal() - int(r.due_day or 0)) > 0
            and int(r.outstanding_kobo) > 0}


def _measured_shops(last_delivery: dict[str, int], restock: dict[str, int],
                    day: date) -> int:
    """Shops past their own restock rhythm, re-derived from the rows by hand."""
    ordinal = day.toordinal()
    out = 0
    for shop_id, rhythm in restock.items():
        last = last_delivery.get(shop_id)
        if last is None or rhythm <= 0 or last > ordinal:
            continue
        if (ordinal - last) // rhythm >= 1:
            out += 1
    return out


def _sku_shops(conn: Any, day: date) -> dict[str, set[str]]:
    """SKU -> the shops that sent it back inside one beat."""
    since = day.toordinal() - brief_mod.BEAT_DAYS
    out: dict[str, set[str]] = {}
    for r in conn.execute(
            sa.select(s.return_lines.c.sku, s.returns.c.shop_id)
            .join(s.returns, sa.and_(s.returns.c.tenant_id == s.return_lines.c.tenant_id,
                                     s.returns.c.return_id == s.return_lines.c.return_id))
            .where(s.returns.c.tenant_id == TENANT,
                   s.returns.c.raised_day >= since,
                   s.returns.c.raised_day <= day.toordinal())).all():
        out.setdefault(str(r.sku), set()).add(str(r.shop_id))
    return out


def figures_trace(ctx: Context) -> bool:
    """Every figure in the message is a row's figure or a sum of rows."""
    allowed_kobo, allowed_ints = _allowed_numbers(ctx.engine, ctx.book_day)
    loop = brief_mod  # keep the module imported for the bands it names
    assert loop is not None
    for raw in NAIRA_RE.findall(ctx.text):
        naira = float(raw.replace(",", ""))
        if int(round(naira * 100)) not in allowed_kobo:
            print(f"  untraceable figure: ₦{raw}")
            return False
    body = NAIRA_RE.sub(" ", ctx.text)
    for token in re.findall(r"\b\d+\b", body):
        if int(token) not in allowed_ints:
            print(f"  untraceable number: {token}")
            return False
    return True


# ------------------------------------------------------------------ scenarios


def _one(*stores: Store) -> list[Store]:
    return list(stores)


def scenarios() -> list[Scenario]:
    return [
        Scenario(
            id="over-limit-ordering",
            stores=_one(
                Store("Alpha Stores", limit_naira=50_000,
                      deliveries=[("2026-10-01", 40_000), ("2026-10-15", 20_000),
                                  ("2026-10-25", 10_000)],
                      invoices=[("2026-10-08", 40_000, 0), ("2026-10-20", 20_000, 0),
                                ("2026-10-30", 10_000, 0)]),
                Store("Beta Kiosk", limit_naira=20_000,
                      deliveries=[("2026-10-02", 30_000)],
                      invoices=[("2026-10-09", 30_000, 0)]),
            ),
            marks=(
                ("the account over its limit and still ordering is flagged",
                 lambda c: has(line_for(c.text, "Alpha Stores", "Money at risk"),
                               "still taking deliveries")),
                ("the account merely over its limit is not flagged",
                 lambda c: "still taking deliveries" not in line_for(c.text, "Beta Kiosk",
                                                                     "Money at risk")),
                ("the merely over-limit account still says it is over",
                 lambda c: has(line_for(c.text, "Beta Kiosk", "Money at risk"),
                               "over its", "limit")),
            ),
        ),
        Scenario(
            id="quiet-before-bigger-balance",
            stores=_one(
                Store("Quiet Stores", restock=2,
                      deliveries=[("2026-10-19", 12_000)],
                      invoices=[("2026-10-18", 12_000, 0)]),
                Store("Loud Stores", restock=7,
                      deliveries=[("2026-10-26", 90_000)],
                      invoices=[("2026-10-20", 90_000, 0)],
                      payments=[("2026-10-27", 1_000, "transfer")]),
            ),
            marks=(
                ("the shop quiet for four cycles ranks above the big fresh balance",
                 lambda c: c.text.index("Quiet Stores") < c.text.index("Loud Stores")),
                ("the quiet shop says how many cycles it missed",
                 lambda c: has(line_for(c.text, "Quiet Stores", "Money at risk"),
                               "4 restock cycles missed")),
            ),
        ),
        Scenario(
            id="age-before-size",
            stores=_one(
                Store("Old Debt Stores", deliveries=[("2026-10-27", 20_000)],
                      invoices=[("2026-08-18", 20_000, 0)]),
                Store("New Big Stores", deliveries=[("2026-10-27", 40_000)],
                      invoices=[("2026-10-17", 40_000, 0)]),
            ),
            marks=(
                ("a 70-day debt outranks one twice its size at 10 days",
                 lambda c: c.text.index("Old Debt Stores") < c.text.index("New Big Stores")),
                ("the old debt says how old it is",
                 lambda c: has(line_for(c.text, "Old Debt Stores", "Money at risk"),
                               "70 days overdue")),
            ),
        ),
        Scenario(
            id="return-three-days-old",
            stores=_one(Store("Return Store", deliveries=[("2026-10-27", 20_000)],
                              invoices=[("2026-10-20", 20_000, 0)],
                              returns=[Return("ret-1", "2026-10-24", (("SKU-A", 1),))])),
            marks=(
                ("a return raised three days ago is escalated in words",
                 lambda c: has(section(c.text, "Returns:"), "3 days open", "Escalated",
                               "waiting on the pickup")),
                ("the escalated return says the shop will dispute the credit note",
                 lambda c: "dispute a credit note" in c.text),
            ),
        ),
        Scenario(
            id="product-problem-flag",
            stores=_one(*[
                Store(f"Shop {index}", deliveries=[("2026-10-26", 10_000)],
                      invoices=[("2026-10-20", 10_000, 0)],
                      returns=[Return(f"ret-{index}", f"2026-10-2{index}", tuple(
                          [("SKU-B", 1)] + ([("SKU-A", 1)] if index < 3 else [])))])
                for index in (4, 5, 6)]),
            marks=(
                ("a SKU returned by three shops is the product's problem",
                 lambda c: has(c.text, "Product problem: Sunrise Noodles",
                               "from 3 shops")),
                ("a SKU returned by two shops is not",
                 lambda c: "Product problem: Golden Soya Oil" not in c.text),
            ),
        ),
        Scenario(
            id="eight-item-cap",
            stores=_one(*[
                Store(f"Debtor {index}", restock=7,
                      deliveries=[(f"2026-10-{10 + index:02d}", 5_000 * index)],
                      invoices=[(f"2026-10-{1 + index:02d}", 5_000 * index, 0)],
                      returns=[Return(f"ret-{index}", "2026-10-25", (("SKU-B", 1),))])
                for index in range(1, 13)]),
            marks=(
                ("the eight-item cap holds",
                 lambda c: len(item_lines(c.text)) == brief_mod.ITEM_CAP),
                ("the count of the rest is stated truthfully",
                 lambda c: f"{brief_mod.ITEM_CAP} items listed, "
                           f"{c.brief.items_total - brief_mod.ITEM_CAP} more in the book."
                           in c.text),
                ("every listed item names an owner and an action",
                 lambda c: all(re.search(r"(Rep [A-Z]|You:)", line)
                               for line in item_lines(c.text))),
            ),
        ),
        Scenario(
            id="quiet-day-says-so",
            stores=_one(Store("Calm Stores", deliveries=[("2026-10-26", 10_000)],
                              invoices=[("2026-10-20", 10_000, 10_000)])),
            marks=(
                ("every section is present even when it has nothing to say",
                 lambda c: has(c.text, "What changed:", "Money at risk: nothing is owed",
                               "Collections today: nothing is overdue",
                               "Returns: nothing is open.",
                               "Coverage: every shop is inside")),
                ("a quiet day lists no items",
                 lambda c: item_lines(c.text) == [] and "items listed" not in c.text),
            ),
        ),
        Scenario(
            id="same-book-same-message",
            stores=_one(Store("Alpha Stores", deliveries=[("2026-10-26", 40_000)],
                              invoices=[("2026-10-20", 40_000, 0)],
                              returns=[Return("ret-1", "2026-10-25", (("SKU-A", 1),))]),
                        Store("Beta Kiosk", restock=2,
                              deliveries=[("2026-10-10", 20_000)],
                              invoices=[("2026-10-09", 20_000, 0)])),
            repeat=True,
            marks=(
                ("the same book on the same day is the same message",
                 lambda c: bool(c.repeat_text) and c.repeat_text == c.text),
            ),
        ),
        Scenario(
            id="odd-figures",
            stores=_one(Store("Odd Stores", deliveries=[("2026-10-06", 7_777)],
                              invoices=[("2026-10-02", 7_777, 1_234)])),
            marks=(
                ("an odd balance is printed as the book holds it",
                 lambda c: "₦6,543" in c.text),
            ),
        ),
        Scenario(
            id="collections-and-unbanked-cash",
            stores=_one(
                Store("Cash Rep Store", rep="rep-1",
                      deliveries=[("2026-10-01", 30_000)],
                      invoices=[("2026-10-10", 30_000, 0)],
                      payments=[("2026-10-20", 5_000, "cash_to_rep")]),
                Store("Plain Store", rep="rep-2",
                      deliveries=[("2026-10-01", 20_000)],
                      invoices=[("2026-10-12", 20_000, 0)]),
            ),
            marks=(
                ("the biggest exposure by amount times age is targeted first",
                 lambda c: c.text.index("Cash Rep Store") < c.text.index("Plain Store")),
                ("unbanked rep cash escalates the item and names the cash",
                 lambda c: has(line_for(c.text, "Cash Rep Store", "Collections today"),
                               "You:", "₦5,000", "banked first")),
                ("a clean account keeps its rep as the owner",
                 lambda c: has(line_for(c.text, "Plain Store", "Collections today"),
                               "Rep Chinelo Eze: collect it today")),
            ),
        ),
        Scenario(
            id="coverage-unknown-separate",
            stores=_one(Store("No Delivery Store"),
                        Store("Lapsed Stores", restock=7,
                              deliveries=[("2026-10-01", 10_000)],
                              invoices=[("2026-10-05", 10_000, 0)],
                              payments=[("2026-10-22", 1_000, "transfer")])),
            marks=(
                ("a shop the book cannot judge is grouped apart and labelled",
                 lambda c: has(c.text, "no delivery on record for 1 shop",
                               "No Delivery Store", "not ranked")),
                ("the unknown shop is never scored as coverage",
                 lambda c: "No Delivery Store" not in
                 "\n".join(line for line in item_lines(c.text))),
                ("the shop that has lapsed is ranked by cycles missed",
                 lambda c: has(c.text, "3 restock cycles missed")),
            ),
        ),
        Scenario(
            id="what-changed-next-day",
            stores=_one(Store("Owes Stores", deliveries=[("2026-10-20", 40_000)],
                              invoices=[("2026-10-10", 40_000, 0)])),
            runs=[
                ("2026-10-26", _one(Store("Owes Stores",
                                          deliveries=[("2026-10-20", 40_000)],
                                          invoices=[("2026-10-10", 40_000, 0)]))),
                ("2026-10-27", _one(Store("Owes Stores",
                                          deliveries=[("2026-10-20", 40_000),
                                                      ("2026-10-27", 15_000)],
                                          invoices=[("2026-10-10", 40_000, 0),
                                                    ("2026-10-27", 15_000, 0)]))),
            ],
            marks=(
                ("the first brief says it is the first",
                 lambda c: "this is the first brief" in c.texts[0]),
                ("the next brief leads with what moved",
                 lambda c: has(c.text, "up ₦15,000", "came in")),
                ("the movement is not a first brief",
                 lambda c: "this is the first brief" not in c.text),
            ),
        ),
    ]


# ------------------------------------------------------------------- the run


def evaluate(only: str = "") -> dict[str, Any]:
    picked = [sc for sc in scenarios() if sc.id.startswith(only)] if only else scenarios()
    marks: dict[str, bool] = {}
    detail: list[tuple[str, str, bool]] = []
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for sc in picked:
            ctx = _run_scenario(sc, workdir)
            if sc.repeat:
                # The same book, imported and read again in a fresh database: the
                # message must be the same byte for byte.
                ctx.repeat_text = _run_scenario(sc, workdir, "-again").text
            for name, check in sc.marks:
                ok = bool(check(ctx))
                marks[f"{sc.id}: {name}"] = ok
                detail.append((sc.id, name, ok))
            traced = figures_trace(ctx)
            marks[f"{sc.id}: every figure traces to a row"] = traced
            detail.append((sc.id, "every figure traces to a row", traced))
            if not all(ok for _id, _name, ok in detail if _id == sc.id):
                print(f"--- {sc.id} (message under test) ---\n{ctx.text}\n---")
    return {"marks": marks, "detail": detail, "scenarios": len(picked)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.api.brief_eval")
    ap.add_argument("--only", default="", help="one scenario by its id prefix")
    args = ap.parse_args(argv)
    started = time.perf_counter()
    r = evaluate(args.only)
    for _id, name, ok in r["detail"]:
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
    passed = sum(1 for value in r["marks"].values() if value)
    total = len(r["marks"])
    print(f"The daily brief: {slice_rate(passed, total)} marks across "
          f"{r['scenarios']} scenario{'s' if r['scenarios'] != 1 else ''} "
          f"({time.perf_counter() - started:.1f}s)")
    print(f"PASSED: {passed == total and total > 0}")
    return exit_code(r["marks"], picked=r["scenarios"])


if __name__ == "__main__":
    sys.exit(main())
