"""The skill catalogue the router picks from (RAI-280).

A helper is config (job spec, skill, tools, risk policy); a skill is one job
that helper does. The router picks ``(helper, skill)`` for every wake-up and
the context builder (RAI-281) loads only that skill's slice of the book.

Descriptions are written for routing: what the sender says or what woke us,
not how the helper works. Names follow the pilot skills where one exists
(``collections``, ``fake-alert-check``, ``drop-confirmation``,
``shop-onboarding``, ``owner-brief``).

``risk`` orders helpers by what they can propose (0 chat, 4 credit). A shop's
text can never raise a route above what its own words, with any injected
instruction removed, would get (``Router`` guard). ``money`` marks skills
where a misroute could tell someone about money they did not ask about or
treat talk as a payment; the eval holds those misroutes at zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

HelperName = Literal["order", "collector", "credit", "stock", "brand", "proof", "mimi"]
Sender = Literal["owner", "shop", "rep", "system"]

HELPERS: tuple[HelperName, ...] = (
    "order", "collector", "credit", "stock", "brand", "proof", "mimi")

# What a helper can do, lowest first. Used by the injection guard.
HELPER_RISK: dict[str, int] = {
    "mimi": 0, "brand": 1, "stock": 1, "order": 2, "proof": 3, "collector": 3, "credit": 4,
}

# Display names for the console's "Handed to ..." system line.
HELPER_TITLE: dict[str, str] = {
    "order": "Order", "collector": "Collector", "credit": "Credit", "stock": "Stock",
    "brand": "Brand", "proof": "Proof", "mimi": "Mimi",
}

CATALOGUE_VERSION = "skills-2026-10-27.1"


@dataclass(frozen=True)
class Skill:
    helper: HelperName
    name: str
    description: str
    senders: tuple[Sender, ...]
    money: bool = False
    # Only a deterministic rule can pick it (no text route leads here).
    rule_only: bool = False
    # Answered by code from the book, with no helper run (docs/HANDOFFS.md):
    # no model body is bound for it, in skills mode either.
    code_only: bool = False

    @property
    def key(self) -> str:
        return f"{self.helper}/{self.name}"


SKILLS: tuple[Skill, ...] = (
    # ---- Order
    Skill("order", "order-intake",
          "The shop wants goods: order, restock, bring cartons, add to the next delivery, "
          "or asks the price or availability of a product.", ("shop", "rep")),
    Skill("order", "drop-confirmation",
          "The shop or rep is answering about a delivery that happened: goods received, "
          "YES, short, damaged, wrong item or leaking.",
          ("shop", "rep")),
    # Owner only, picked by a router rule (never by the model, so the model's
    # catalogue text and its recorded answers are unchanged): "any orders
    # now?", "show me the orders". Answered by code from the book with a list
    # card (``mimi.api.cards``, docs/HANDOFFS.md).
    Skill("order", "open-orders",
          "The owner asks which shop orders are open or waiting now: any orders, new orders, "
          "orders waiting for approval.", ("owner",), rule_only=True, code_only=True),
    Skill("order", "delivery-status",
          "Goods not arrived yet: when is the van coming, where is my order, delivery late.",
          ("shop", "rep", "owner")),
    # ---- Collector
    Skill("collector", "collections",
          "A balance question or a disagreement about it: what do I owe, how much remains, "
          "which account do I pay into, that amount is wrong. Also the owner asking what one "
          "shop owes.", ("shop", "owner", "rep"), money=True),
    Skill("collector", "payment-check",
          "Someone says a payment was made, in words without a picture: I have paid, I sent "
          "the transfer, I gave the rep cash; or the owner asks whether a shop's payment "
          "landed.", ("shop", "rep", "owner"), money=True),
    Skill("collector", "promise-to-pay",
          "The shop says when it will pay or asks for more time: I will pay Friday, give me "
          "till month end, money will come next week.", ("shop", "rep"), money=True),
    Skill("collector", "chase",
          "The owner asks to chase debtors: who owes me, remind everyone overdue, chase "
          "shops over 50k.", ("owner",), money=True),
    # ---- Proof
    Skill("proof", "fake-alert-check",
          "A payment screenshot, a debit alert or a pasted bank SMS that needs checking "
          "before anyone counts it as paid.", ("shop", "rep", "owner"), money=True),
    Skill("proof", "bank-credit",
          "A bank-feed credit arrived and must be matched to a shop and invoice.",
          ("system",), money=True, rule_only=True),
    # ---- Credit
    Skill("credit", "credit-request",
          "The shop wants more credit: raise my limit, give me goods now and I pay later, "
          "credit bump, sometimes with a shelf photo.", ("shop", "rep"), money=True),
    Skill("credit", "credit-review",
          "The owner asks about a shop's risk or limit: should I give them more credit, is "
          "this shop safe, review credit limits.", ("owner",), money=True),
    # ---- Stock
    Skill("stock", "stock-count",
          "The shop says what is left on its shelf or answers a stock count (0-2, 3-5, 6+, "
          "it has finished, two cartons remain), or sends a shelf photo to count.",
          ("shop", "rep")),
    Skill("stock", "low-stock-report",
          "The owner asks which shops are running low or what stock to push.", ("owner",)),
    # ---- Brand
    Skill("brand", "promo-info",
          "Questions about brand promos, discounts from the manufacturer, new products, "
          "posters or campaigns.", ("shop", "rep", "owner")),
    Skill("brand", "brand-email",
          "The owner asks for an email or letter to a brand, manufacturer or bank (always a "
          "draft for the owner).", ("owner",)),
    # ---- Mimi (Chief of Staff)
    Skill("mimi", "shop-onboarding",
          "A new or unknown number, an invite, what is Mimi, what do you share, or a shop "
          "asking to stop or pause messages.", ("shop", "rep")),
    Skill("mimi", "owner-brief",
          "The owner asks how the business is doing today or yesterday, or the morning brief.",
          ("owner", "system")),
    Skill("mimi", "chat",
          "Greetings, thanks, small talk and off-topic messages with nothing to do.",
          ("shop", "rep", "owner")),
    Skill("mimi", "escalate-owner",
          "A complaint about a rep or the service, a threat, a legal or personal matter, or "
          "anything Mimi should not handle alone: the owner decides.", ("shop", "rep")),
    Skill("mimi", "stop",
          "The owner says stop: pause the work that is running now.", ("owner",),
          rule_only=True),
)

BY_KEY: dict[str, Skill] = {s.key: s for s in SKILLS}
MONEY_SKILLS: frozenset[str] = frozenset(s.key for s in SKILLS if s.money)
TEXT_SKILLS: tuple[Skill, ...] = tuple(s for s in SKILLS if not s.rule_only)


def skill(key: str) -> Skill:
    return BY_KEY[key]


def allowed_for(sender: str) -> tuple[Skill, ...]:
    """Skills a text from this sender may route to (rules aside)."""
    return tuple(s for s in TEXT_SKILLS if sender in s.senders)


def default_skill(helper: str, sender: str = "owner") -> str:
    """The skill an @mention lands on when the words do not pick one."""
    for s in TEXT_SKILLS:
        if s.helper == helper and sender in s.senders:
            return s.key
    return f"{helper}/{next(s.name for s in SKILLS if s.helper == helper)}"


def catalogue_text(sender: str) -> str:
    """The catalogue as the routing model sees it, for one sender kind."""
    lines = []
    for s in allowed_for(sender):
        lines.append(f"- {s.key}: {s.description}")
    return "\n".join(lines)
