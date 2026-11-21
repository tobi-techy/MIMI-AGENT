"""Types every part of Mimi shares.

``ShopScope`` names which distributor (tenant) and which shop an intent is
about. The runner checks the tenant on every outbound intent, so a job for
one distributor can never propose anything for another.

Money is integer kobo everywhere (100 kobo = 1 naira). ``Kobo`` is only a
readable alias for ``int``: no float ever holds an amount.
"""

from __future__ import annotations

from dataclasses import dataclass

Kobo = int

#: The states a return moves through, in order: raised and waiting on the owner
#: (``drafted``), approved (``approved``), with the rep (``collected``), counted
#: and examined at the depot (``inspected``), credited and applied (``settled``),
#: or declined (``cancelled``). One vocabulary for a return from any source; the
#: transition table that keeps the order is ``mimi.returns.ledger``.
RETURN_STATUSES: tuple[str, ...] = (
    "drafted", "approved", "collected", "inspected", "settled", "cancelled")

#: A return in one of these is still being worked on; anything else is closed.
RETURN_OPEN_STATUSES: frozenset[str] = frozenset({
    "drafted", "approved", "collected", "inspected"})

#: Why a return came back, from the visit-record contract's closed list plus the
#: three reasons only a return has. A free-text reason is not a vocabulary, and
#: a reason nobody chose from cannot be counted.
RETURN_REASONS: tuple[str, ...] = (
    "shut", "no stock needed", "refused", "damaged", "expired", "wrong_sku")


@dataclass(frozen=True)
class ShopScope:
    """The tenant (distributor) and shop an intent belongs to."""

    tenant_id: str
    shop_id: str

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.shop_id:
            raise ValueError("ShopScope needs a tenant_id and a shop_id")
