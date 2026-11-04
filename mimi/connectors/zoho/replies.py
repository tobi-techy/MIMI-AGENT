"""A failed Zoho Books reply is never an empty book.

Composio answers a malformed call with a payload, not an exception: a read
sent without ``organization_id`` comes back as
``{"code": null, "message": "Invalid request data provided - Following
fields are missing: {'organization_id'}"}``. Mapping that to zero rows
turned three separate bugs into a confident zero on the owner's screen,
where "Zoho says this shop owes nothing" and "we never asked Zoho" looked
identical. Every Zoho Books read and every payment read-back therefore
passes through :func:`check_reply` before a single row is mapped.

The rule is Zoho's own: a successful call carries ``code: 0``. Anything
else, including no code at all, raises with what Zoho said and what to do
next, so a read that returns nothing means the book holds nothing.
"""

from __future__ import annotations

from typing import Any

from mimi.connectors.base import ConnectorError


def check_reply(action: str, raw: Any) -> dict[str, Any]:
    """Return the reply when Zoho answered ``code: 0``, else raise.

    ``action`` names the read in Mimi's own words (``zoho_books_invoices``),
    never a secret and never the raw arguments.
    """
    if not isinstance(raw, dict) or not raw:
        raise ConnectorError(
            f"Zoho answered {action} with nothing readable, so nothing was read.",
            next_step=_next_step(""))
    code = raw.get("code")
    message = str(raw.get("message") or "").strip()
    if code is None:
        said = f"Zoho answered {action} without a result code, so nothing was read"
        raise ConnectorError(
            f"{said}: {message}" if message else f"{said}.",
            next_step=_next_step(message))
    if not isinstance(code, int) or code != 0:
        said = f"Zoho refused {action} (code {code})"
        raise ConnectorError(
            f"{said}: {message}" if message else f"{said}.",
            next_step=_next_step(message))
    return raw


def _next_step(message: str) -> str:
    said = message.lower()
    if "organization_id" in said or "organization id" in said:
        return ("Pass organization_id (ZOHO_BOOKS_ORGANIZATION_ID) to this read "
                "and retry.")
    if ("scope" in said or "authorized" in said or "unauthorized" in said
            or "denied" in said or "expired" in said):
        return "Reconnect Zoho Books for this tenant, then retry the read."
    if "invalid" in said or "missing" in said:
        return "Fix what Zoho rejected, then retry the read."
    return "Retry the read; if it repeats, reconnect Zoho Books."
