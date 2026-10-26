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


@dataclass(frozen=True)
class ShopScope:
    """The tenant (distributor) and shop an intent belongs to."""

    tenant_id: str
    shop_id: str

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.shop_id:
            raise ValueError("ShopScope needs a tenant_id and a shop_id")
