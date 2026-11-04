"""The returns ledger's arithmetic (feat/returns-and-exposure): pure code, no model.

A distributor's returns cost him most, because he pays twice, delivering and
then collecting. This module holds the rules the API applies, so every figure
and every check is code:

* the lifecycle (``drafted`` to ``settled``, or ``cancelled``) and which
  moves are allowed from which status (``can_move``);
* the line values, the recovered value (only lines that sell again or go back
  to the supplier) and the saving against the owner's configured pickup cost
  (``line_value_kobo``, ``recovered_kobo``, ``saving_kobo``);
* how a settlement credit comes off the shop's open invoices, oldest first,
  and what is left on the shop's account when it owed nothing
  (``credit_plan``);
* the plain sentences a person reads, built without any internal id
  (``scrub`` removes any that a caller passes through by accident).

Nothing here reads a database, calls a model or moves money. Money is integer
kobo. The rep collects; Mimi never moves stock or money.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from mimi import owed

Status = Literal["drafted", "approved", "collected", "inspected", "settled", "cancelled"]

DRAFTED: Status = "drafted"
APPROVED: Status = "approved"
COLLECTED: Status = "collected"
INSPECTED: Status = "inspected"
SETTLED: Status = "settled"
CANCELLED: Status = "cancelled"

STATUSES: tuple[Status, ...] = (DRAFTED, APPROVED, COLLECTED, INSPECTED, SETTLED, CANCELLED)

# What a status may become. A settled return with an undone credit goes back
# to inspected, so inspected can be reached from collected and from settled.
TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    DRAFTED: (APPROVED, CANCELLED),
    APPROVED: (COLLECTED, CANCELLED),
    COLLECTED: (INSPECTED,),
    INSPECTED: (SETTLED,),
    SETTLED: (INSPECTED,),
    CANCELLED: (),
}

Condition = Literal["resellable", "damaged", "expired"]
Disposition = Literal["resell", "write_off", "return_to_supplier"]
CONDITIONS: tuple[Condition, ...] = ("resellable", "damaged", "expired")
DISPOSITIONS: tuple[Disposition, ...] = ("resell", "write_off", "return_to_supplier")
# Only these recovered value; a write-off recovers nothing (the goods are gone).
RECOVERING_DISPOSITIONS: tuple[Disposition, ...] = ("resell", "return_to_supplier")

LEDGER_VERSION = "returns-v0.1"

# The one string the settlement approval carries (tool, intent_kind, payload
# kind), so the API and the card agree. Rides the approvals route like a Zoho
# payment and carries no money verb.
CREDIT_KIND = "return_credit"

# Any internal id shape a person must never read in a reason or a note.
# ``crn`` is a credit note (``ledger.credit_id_for``); the hex run it carries is
# not caught on its own, because the underscore before it is a word character.
_ID_LIKE = re.compile(
    r"\b(?:shop|dlv|apr|ret|stk|inv|crn)[-_][A-Za-z0-9]+\b|\bshop_\w+\b|_[a-z]*kobo\b"
    r"|\b[0-9a-f]{16,}\b")


def can_move(current: str, to: str) -> bool:
    """Whether the lifecycle allows ``current`` to become ``to``."""
    return to in TRANSITIONS.get(current, ())


def scrub(text: str) -> str:
    """A person-read string with any internal id shape taken out, and no
    em or en dash (the style rule every message follows)."""
    out = _ID_LIKE.sub("", text)
    out = out.replace("\u2014", ",").replace("\u2013", ",")
    return re.sub(r"\s+", " ", out).strip()


def line_value_kobo(quantity: int, unit_price_kobo: int) -> int:
    return max(0, int(quantity)) * max(0, int(unit_price_kobo))


def recovered_kobo(lines: Iterable[Mapping[str, Any]]) -> int:
    """What came back able to sell again or go to the supplier, in kobo.
    Lines written off recover nothing."""
    return sum(int(ln.get("value_returned_kobo") or 0) for ln in lines
               if ln.get("disposition") in RECOVERING_DISPOSITIONS)


def saving_kobo(recovered: int, pickup_cost_kobo: int | None) -> int | None:
    """The return's net value against the owner's pickup cost, or ``None`` when
    the owner set no pickup cost (nothing is assumed in its place)."""
    if pickup_cost_kobo is None:
        return None
    return int(recovered) - int(pickup_cost_kobo)


def saving_basis(recovered: int, pickup_cost_kobo: int | None) -> str:
    """The assumption the ``saving_kobo`` figure rests on, in plain words."""
    if pickup_cost_kobo is None:
        return ("No pickup cost is set, so no saving is claimed: set what a "
                "collection trip costs you to see the net figure.")
    return (f"{owed.naira(recovered)} recovered off the lines that sell again or go "
            f"back to the supplier, less the {owed.naira(pickup_cost_kobo)} pickup cost you "
            "set. Goods written off are counted at nothing.")


def credit_plan(amount_kobo: int, receivables: Sequence[Mapping[str, Any]]
                ) -> tuple[list[dict[str, Any]], int, int]:
    """Where a credit of ``amount_kobo`` comes off the shop's book.

    Returns ``(applications, applied_kobo, on_account_kobo)``: the open
    invoices it reduces, oldest first (``position``, then delivery id), what
    it took off them, and the part held on the shop's account because it owed
    less than the credit."""
    left = max(0, int(amount_kobo))
    rows = sorted(receivables, key=lambda r: (int(r.get("position") or 0),
                                              str(r.get("delivery_id") or "")))
    applications: list[dict[str, Any]] = []
    for r in rows:
        if left <= 0:
            break
        out = int(r.get("outstanding_kobo") or 0)
        take = min(left, out)
        if take <= 0:
            continue
        applications.append({"delivery_id": str(r.get("delivery_id") or ""),
                             "amount_kobo": take})
        left -= take
    applied = int(amount_kobo) - left
    return applications, applied, left


def return_id_for(tenant_id: str, key: str) -> str:
    return "ret_" + hashlib.sha256(f"{tenant_id}\x00{key}".encode()).hexdigest()[:20]


def credit_id_for(tenant_id: str, return_id: str, attempt: int) -> str:
    return "crn_" + hashlib.sha256(
        f"{tenant_id}\x00{return_id}\x00{attempt}".encode()).hexdigest()[:20]


def return_reason(shop_name: str, lines: Sequence[Mapping[str, Any]]) -> str:
    """One plain sentence for the whole return, no ids."""
    first = lines[0] if lines else {}
    what = f"{first.get('quantity_drafted', 0)} {first.get('unit', '')} {first.get('item', '')}".strip()
    if len(lines) > 1:
        what = f"{what} and {len(lines) - 1} more {'line' if len(lines) == 2 else 'lines'}"
    return scrub(f"Return from {shop_name}: {what}.")


def line_reason(item: str, unit: str, quantity: int, expires_on: str = "",
                near_expiry: bool = True) -> str:
    """Why a line is coming back, in plain words with no ids."""
    what = f"{quantity} {unit} {item}".strip()
    if near_expiry and expires_on:
        return scrub(f"{what} may not sell before {_date_text(expires_on)}.")
    return scrub(f"{what} is more than the shop needs.")


def _date_text(iso: str) -> str:
    try:
        y, m, d = (int(p) for p in iso.split("-", 2)[:3])
    except (TypeError, ValueError):
        return iso
    return f"{d} {_MONTH[m - 1]} {y}"


_MONTH = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
