"""The allowlist: every outside action Mimi may take (RAI-277).

Each row names a Mimi action, whether it reads or writes, the Composio
slug it runs as, the pinned toolkit version, the undo action for a write,
and the ``policy.decide`` action type for the approval gate (RAI-323
lands the rulebook; until then every write defaults to ask the owner).

Anything not listed here is refused in code. In particular this list
never holds deletes (except the two undo pairs below), voids,
write-offs, bank account or bank transaction actions, vendor payments,
refunds, or email and remind actions (they message shops). Mimi never
moves money.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Action:
    """One allowlisted outside action."""

    action: str
    kind: str  # "read" or "write"
    toolkit: str
    slug: str
    version: str
    undo_action: str = ""
    policy_action: str = ""


ZOHO_BOOKS_VERSION = "20261026_00"
ZOHO_CRM_VERSION = "20261026_00"
SHEETS_VERSION = "20261026_00"

ACTIONS: tuple[Action, ...] = (
    # Zoho Books reads.
    Action("zoho_books_organizations", "read", "ZOHO_BOOKS",
           "ZOHO_BOOKS_LIST_ORGANIZATIONS", ZOHO_BOOKS_VERSION),
    Action("zoho_books_contacts", "read", "ZOHO_BOOKS",
           "ZOHO_BOOKS_LIST_CONTACTS", ZOHO_BOOKS_VERSION),
    Action("zoho_books_contact", "read", "ZOHO_BOOKS",
           "ZOHO_BOOKS_GET_CONTACT", ZOHO_BOOKS_VERSION),
    Action("zoho_books_invoices", "read", "ZOHO_BOOKS",
           "ZOHO_BOOKS_LIST_INVOICES", ZOHO_BOOKS_VERSION),
    Action("zoho_books_invoice", "read", "ZOHO_BOOKS",
           "ZOHO_BOOKS_GET_INVOICE", ZOHO_BOOKS_VERSION),
    Action("zoho_books_customer_payments", "read", "ZOHO_BOOKS",
           "ZOHO_BOOKS_LIST_CUSTOMER_PAYMENTS", ZOHO_BOOKS_VERSION),
    Action("zoho_books_invoice_payments", "read", "ZOHO_BOOKS",
           "ZOHO_BOOKS_LIST_INVOICE_PAYMENTS", ZOHO_BOOKS_VERSION),
    Action("zoho_books_items", "read", "ZOHO_BOOKS",
           "ZOHO_BOOKS_LIST_ITEMS", ZOHO_BOOKS_VERSION),
    # Zoho Books writes (owner-approved only; each with its undo).
    Action("zoho_books_invoice_comment", "write", "ZOHO_BOOKS",
           "ZOHO_BOOKS_ADD_INVOICE_COMMENT", ZOHO_BOOKS_VERSION,
           undo_action="zoho_books_invoice_comment_delete",
           policy_action="connector.zoho_books.comment"),
    Action("zoho_books_invoice_comment_delete", "write", "ZOHO_BOOKS",
           "ZOHO_BOOKS_DELETE_INVOICE_COMMENT", ZOHO_BOOKS_VERSION,
           policy_action="connector.zoho_books.comment_undo"),
    Action("zoho_books_contact_update", "write", "ZOHO_BOOKS",
           "ZOHO_BOOKS_UPDATE_CONTACT", ZOHO_BOOKS_VERSION,
           undo_action="zoho_books_contact_update",
           policy_action="connector.zoho_books.contact"),
    # Zoho Books customer payment over Composio proxy execute (this issue):
    # records a payment the shop already made; Mimi still never moves money.
    # Only ``mimi/connectors/zoho/payments.py`` may use this row.
    Action("zoho_books_customer_payment", "write", "ZOHO_BOOKS",
           "PROXY_CUSTOMER_PAYMENTS", ZOHO_BOOKS_VERSION,
           undo_action="zoho_books_customer_payment_delete",
           policy_action="connector.zoho_books.payment"),
    Action("zoho_books_customer_payment_delete", "write", "ZOHO_BOOKS",
           "PROXY_CUSTOMER_PAYMENTS_DELETE", ZOHO_BOOKS_VERSION,
           policy_action="connector.zoho_books.payment_undo"),
    # Zoho CRM reads.
    Action("zoho_crm_accounts", "read", "ZOHO",
           "ZOHO_LIST_ACCOUNTS", ZOHO_CRM_VERSION),
    Action("zoho_crm_accounts_search", "read", "ZOHO",
           "ZOHO_SEARCH_ACCOUNTS", ZOHO_CRM_VERSION),
    Action("zoho_crm_contacts", "read", "ZOHO",
           "ZOHO_LIST_CONTACTS", ZOHO_CRM_VERSION),
    Action("zoho_crm_contacts_search", "read", "ZOHO",
           "ZOHO_SEARCH_CONTACTS", ZOHO_CRM_VERSION),
    Action("zoho_crm_record", "read", "ZOHO",
           "ZOHO_GET_RECORD", ZOHO_CRM_VERSION),
    # Zoho CRM write (owner-approved only, with its undo).
    Action("zoho_crm_note", "write", "ZOHO",
           "ZOHO_CREATE_NOTE", ZOHO_CRM_VERSION,
           undo_action="zoho_crm_note_delete",
           policy_action="connector.zoho_crm.note"),
    Action("zoho_crm_note_delete", "write", "ZOHO",
           "ZOHO_DELETE_NOTE", ZOHO_CRM_VERSION,
           policy_action="connector.zoho_crm.note_undo"),
    # Google Sheets reads only. This issue never writes to shop sheets.
    Action("sheets_spreadsheet_info", "read", "GOOGLESHEETS",
           "GOOGLESHEETS_GET_SPREADSHEET_INFO", SHEETS_VERSION),
    Action("sheets_sheet_names", "read", "GOOGLESHEETS",
           "GOOGLESHEETS_GET_SHEET_NAMES", SHEETS_VERSION),
    Action("sheets_values", "read", "GOOGLESHEETS",
           "GOOGLESHEETS_VALUES_GET", SHEETS_VERSION),
    Action("sheets_values_batch", "read", "GOOGLESHEETS",
           "GOOGLESHEETS_BATCH_GET", SHEETS_VERSION),
)


def get(action: str) -> Action | None:
    """The allowlist row for a Mimi action, or None when not allowlisted."""
    for row in ACTIONS:
        if row.action == action:
            return row
    return None


def is_read(action: str) -> bool:
    row = get(action)
    return row is not None and row.kind == "read"


def is_write(action: str) -> bool:
    row = get(action)
    return row is not None and row.kind == "write"


# Slugs no allowlist row may ever name: the hard refusals behind ``get``.
REFUSED_PREFIXES: tuple[str, ...] = (
    "DELETE_", "VOID_", "WRITE_OFF_", "REFUND_",
    "EMAIL_", "REMIND_", "BANK_", "VENDOR_PAYMENT_",
)
