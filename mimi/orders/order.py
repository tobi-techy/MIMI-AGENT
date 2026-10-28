"""Prices, totals, credit and every reply text of an order, in code (RAI-302).

Nothing here calls a model. The model proposes lines (the shop's words and
the quantity in them); ``mimi.orders.tools`` checks them and calls into here
for the figures and the words Mimi sends:

* ``price_line``: the catalogue price for the live day (``Sku.price_on``)
  times the quantity, integer kobo.
* ``credit_view``: available credit is the distributor's limit minus what the
  shop owes on open invoices and minus its other orders already waiting or
  approved on credit. An order above it is flagged with the shortfall; it
  still goes to the owner, who decides. Mimi never sets or raises a limit.
* Reply texts: the confirmation, the one question, a price answer, the
  approved notice, the "already decided" answer. Plain English, every naira
  figure from these functions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from mimi.harness.loop import naira_text
from mimi.stock.items import display_name, unit_word
from mimi.world.model import Sku

Payment = Literal["credit", "pays_now"]
UnclearReason = Literal["which", "how_many", "unit", "family"]
# The most of one line a shop can order in one message without a question.
MAX_QTY = 200


@dataclass(frozen=True)
class Priced:
    sku: str
    item: str
    unit: str
    quantity: int
    unit_price_kobo: int
    line_total_kobo: int
    source: Literal["shop", "repeat"] = "shop"
    as_written: str = ""

    def text(self) -> str:
        return (f"{self.quantity} {unit_word(self.unit, self.quantity)} {self.item}: "
                f"{naira_text(self.line_total_kobo)}")


@dataclass(frozen=True)
class Option:
    sku: str
    item: str
    unit: str
    unit_price_kobo: int

    def text(self) -> str:
        return f"{self.item} ({naira_text(self.unit_price_kobo)} a {unit_word(self.unit, 1)})"


@dataclass(frozen=True)
class Unclear:
    as_written: str
    item: str
    reason: UnclearReason
    options: tuple[Option, ...] = ()
    label: str = ""


@dataclass
class Credit:
    limit_kobo: int
    owed_kobo: int
    committed_kobo: int
    available_kobo: int
    payment: Payment
    over_credit: bool
    over_by_kobo: int


@dataclass
class Quote:
    lines: list[Priced] = field(default_factory=list)
    unclear: list[Unclear] = field(default_factory=list)
    not_carried: list[str] = field(default_factory=list)

    @property
    def total_kobo(self) -> int:
        return sum(ln.line_total_kobo for ln in self.lines)


def price_line(sku: Sku, quantity: int, live_day: int, rise_day: int | None, *,
               source: Literal["shop", "repeat"] = "shop", as_written: str = "") -> Priced:
    price = sku.price_on(live_day, rise_day if rise_day is not None else 10**9)
    return Priced(sku.sku, display_name(sku.name), sku.unit, int(quantity), price,
                  int(quantity) * price, source, as_written)


def option(sku: Sku, live_day: int, rise_day: int | None) -> Option:
    return Option(sku.sku, display_name(sku.name), sku.unit,
                  sku.price_on(live_day, rise_day if rise_day is not None else 10**9))


def merge(lines: Sequence[Priced]) -> list[Priced]:
    """One line per sku (two lines of the same item add up), first order kept."""
    out: dict[str, Priced] = {}
    for ln in lines:
        if ln.sku in out:
            p = out[ln.sku]
            q = p.quantity + ln.quantity
            out[ln.sku] = Priced(p.sku, p.item, p.unit, q, p.unit_price_kobo,
                                 q * p.unit_price_kobo, p.source, p.as_written)
        else:
            out[ln.sku] = ln
    return list(out.values())


def credit_view(limit_kobo: int, owed_kobo: int, committed_kobo: int, total_kobo: int,
                payment: Payment) -> Credit:
    available = limit_kobo - owed_kobo - committed_kobo
    over = payment == "credit" and total_kobo > available
    return Credit(limit_kobo, owed_kobo, committed_kobo, available, payment, over,
                  max(0, total_kobo - max(available, 0)) if over else 0)


# ----------------------------------------------------------------- texts


def lines_text(lines: Sequence[Priced]) -> str:
    return "\n".join(ln.text() for ln in lines)


def confirm_text(q: Quote, credit: Credit, distributor: str, *, left_for_rep: Sequence[str] = (),
                 revised: bool = False) -> str:
    head = "Updated. Your order now:" if revised else "Got it. Your order:"
    pay = " (you pay before delivery)" if credit.payment == "pays_now" else " on credit"
    parts = [head, lines_text(q.lines), f"Total {naira_text(q.total_kobo)}{pay}."]
    if q.not_carried:
        parts.append(f"We don't carry {_join(q.not_carried)}.")
    if left_for_rep:
        parts.append(f"Your rep will confirm {_join(left_for_rep)} with you.")
    if credit.over_credit:
        parts.append(f"This is {naira_text(credit.over_by_kobo)} above your available "
                     f"credit, so {distributor} will decide. I've sent it to them.")
    else:
        parts.append(f"I've sent it to {distributor} to approve.")
    parts.append("Reply to change anything before they approve.")
    return "\n".join(parts)


def approved_text(order_id: str, total_kobo: int, distributor: str) -> str:
    return (f"{distributor} approved your order {order_id} ({naira_text(total_kobo)}). "
            "It comes with the next van.")


def question_text(unclear: Sequence[Unclear]) -> str:
    asks = [_ask(u) for u in unclear[:2]]
    return "Before I send your order: " + " Also, ".join(asks)


def _ask(u: Unclear) -> str:
    if u.reason == "how_many":
        if u.options and len(u.options) == 1:
            o = u.options[0]
            return f"how many {unit_word(o.unit)} of {o.item} do you want?"
        return f"how many of the {u.label or u.item} do you want, and which one?" \
            if len(u.options) > 1 else f"how many {u.label or u.item} do you want?"
    if u.reason == "unit" and u.options:
        o = u.options[0]
        return (f"{o.item} comes in {unit_word(o.unit)} ({naira_text(o.unit_price_kobo)} a "
                f"{unit_word(o.unit, 1)}). How many {unit_word(o.unit)} do you want?")
    opts = [o.text() for o in u.options]
    choice = opts[0] if len(opts) == 1 else ", ".join(opts[:-1]) + " or " + opts[-1]
    return f"which {u.label or u.item} do you want: {choice}?"


def price_text(options: Sequence[Option]) -> str:
    rows = [f"{o.item}: {naira_text(o.unit_price_kobo)} a {unit_word(o.unit, 1)}"
            for o in options]
    return "Today's price" + ("s" if len(rows) > 1 else "") + ":\n" + "\n".join(rows) + \
        "\nTell me how many you want and I'll send the order."


def not_carried_text(words: Sequence[str], distributor: str) -> str:
    return (f"Sorry, {distributor} doesn't carry {_join(words)}. Tell me what else you need "
            "and I'll send the order.")


def decided_text(status: str, distributor: str) -> str:
    did = "approved" if status == "approved" else "turned down"
    return (f"That order was already {did} by {distributor}, so I can't change it. "
            "Send me anything extra as a new order.")


def nothing_text() -> str:
    return "Tell me what you want to order (item and how many) and I'll send it."


def _join(xs: Sequence[str]) -> str:
    xs = [x for x in xs if x]
    if len(xs) <= 1:
        return xs[0] if xs else ""
    return ", ".join(xs[:-1]) + " and " + xs[-1]
