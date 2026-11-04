"""Match Mimi's shops to Zoho Books customers (the two books comparison).

Zoho Books is a separate book with its own ids. A real organization numbers
its contacts (``"513..."``); the demo's fake book writes ``zc-<shop_id>``.
So a shop is matched here, in code, in this order:

1. a contact id of ``zc-<shop_id>``, the fake book's convention, kept
   exactly as it was;
2. otherwise the Zoho customer whose name equals the shop name, compared
   case and whitespace insensitively after collapsing internal runs of
   spaces, and only when exactly one customer carries that name.

A name that two Zoho customers share, or one customer name that two Mimi
shops carry, matches nobody: the shop reads unknown rather than a coin
toss. Money Zoho holds for a customer no shop claims keeps its own row
(``zoho:<name>``, with the customer id appended when two customers share a
name), so it never leaves the total.

Nothing in this module reads a transport. The same rules therefore run
against the fake book and a real organization: behaviour never depends on
which transport is in play.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

ZOHO_ONLY_PREFIX = "zoho:"
FAKE_ID_PREFIX = "zc-"


def norm_name(name: str) -> str:
    """A name for comparison: case folded, whitespace collapsed, trimmed."""
    return " ".join(str(name or "").split()).casefold()


@dataclass(frozen=True)
class ZohoCustomer:
    """One Zoho Books customer and what Zoho says it owes, in kobo."""

    customer_id: str
    name: str
    outstanding_kobo: int


@dataclass(frozen=True)
class ShopMatch:
    """What the matcher decided, for the agreement screen and the payment path.

    ``per_shop`` and ``matched_by`` are keyed by Mimi shop id; ``zoho_only``
    is keyed by the screen row key (``zoho:<name>``) and holds that
    customer's name and kobo; ``by_id`` names the Zoho customer behind each
    matched shop, which an owner-approved payment needs; ``ambiguous`` lists
    the shops left unmatched because more than one Zoho customer carried
    their name."""

    per_shop: dict[str, int] = field(default_factory=dict)
    matched_by: dict[str, str] = field(default_factory=dict)
    by_id: dict[str, str] = field(default_factory=dict)
    zoho_only: dict[str, tuple[str, int]] = field(default_factory=dict)
    ambiguous: tuple[str, ...] = ()


def match_shops(shops: Mapping[str, str],
                customers: Sequence[ZohoCustomer]) -> ShopMatch:
    """Match shops (``shop_id -> name``) to Zoho customers by the rules above."""
    per_shop: dict[str, int] = {}
    matched_by: dict[str, str] = {}
    by_id: dict[str, str] = {}
    claimed: set[str] = set()

    # 1. The fake book's convention: ``zc-<shop_id>``. Every contact that
    # carries it is the shop's, and the shop's outstanding is their sum,
    # exactly as the screen read it before real Zoho mattered.
    for c in customers:
        sid = c.customer_id[len(FAKE_ID_PREFIX):] if c.customer_id.startswith(
            FAKE_ID_PREFIX) else ""
        if not sid or sid not in shops:
            continue
        per_shop[sid] = per_shop.get(sid, 0) + int(c.outstanding_kobo)
        matched_by[sid] = "id"
        by_id[sid] = c.customer_id
        claimed.add(c.customer_id)

    # 2. The name, only when it identifies exactly one customer and one shop.
    shops_by_name: dict[str, list[str]] = {}
    for sid, name in shops.items():
        shops_by_name.setdefault(norm_name(name), []).append(sid)
    customers_by_name: dict[str, list[ZohoCustomer]] = {}
    for c in customers:
        if c.customer_id not in claimed:
            customers_by_name.setdefault(norm_name(c.name), []).append(c)

    ambiguous: list[str] = []
    for sid, name in sorted(shops.items()):
        if sid in per_shop:
            continue
        key = norm_name(name)
        cands = customers_by_name.get(key, [])
        if not key or not cands:
            continue
        if len(cands) > 1 or len(shops_by_name.get(key, [])) > 1:
            ambiguous.append(sid)
            continue
        c = cands[0]
        per_shop[sid] = int(c.outstanding_kobo)
        matched_by[sid] = "name"
        by_id[sid] = c.customer_id
        claimed.add(c.customer_id)

    # 3. Money Zoho holds that no shop claims keeps its own row.
    zoho_only: dict[str, tuple[str, int]] = {}
    for c in customers:
        if c.customer_id in claimed or int(c.outstanding_kobo) == 0:
            continue
        name = str(c.name or c.customer_id)
        key = f"{ZOHO_ONLY_PREFIX}{name}"
        if key in zoho_only:
            key = f"{ZOHO_ONLY_PREFIX}{name}#{c.customer_id}"
        zoho_only[key] = (name, int(c.outstanding_kobo))

    return ShopMatch(per_shop=per_shop, matched_by=matched_by, by_id=by_id,
                     zoho_only=zoho_only, ambiguous=tuple(ambiguous))
