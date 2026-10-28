"""Risk tiers, read from the system of record's policy, never from the model.

``RiskPolicy`` is Go's policy endpoint. ``GoPolicy`` is its stand-in with the
same fixed table and amount rules, so tests and the sim behave as production
will. Two rules hold for every helper at every trust level:

* moving money and changing bank details are tier ``never`` (RAI-288);
* a tool Go has no policy row for is ``never`` (fail closed).

The amount rules compare integer kobo computed by code (the tool's own
output), never a figure the model wrote.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from mimi.harness.types import Tier

# Never, for every helper, at every trust level. A toolset that lists one of
# these fails at startup; a model that calls one is refused.
NEVER_TOOLS: frozenset[str] = frozenset(
    {
        "transfer_money",
        "send_money",
        "pay_out",
        "refund_payment",
        "change_bank_details",
        "update_bank_account",
        "write_off_debt",
        "change_invoice_amount",
        "extend_due_date",
        "give_discount",
        "request_pin_or_otp",
        # Credit (RAI-271): the distributor sets credit; Mimi never does.
        "set_credit_limit",
        "change_credit_limit",
        "raise_credit_limit",
        "approve_credit_request",
    }
)

BASE_TIERS: dict[str, Tier] = {
    # Collector reads.
    "get_balance": "auto",
    "get_invoices": "auto",
    "get_payment_history": "auto",
    "get_account_number": "auto",
    "check_payment": "auto",
    # Collector writes (intents). Amount and tone rules below can raise them.
    "create_exact_amount_account": "notify",
    "send_receipt": "auto",
    "propose_reminder_plan": "approve",
    "send_reminder": "notify",
    "set_followup": "auto",
    "ask_helper": "auto",
    "notify_rep": "auto",
    "notify_owner": "auto",
    # Harness built-ins.
    "send_to_user": "auto",
    "ask_question": "auto",
    "update_plan": "auto",
    # Proof (minimal, for the connector-down rule).
    "check_bank_feed": "auto",
    "open_bank_portal": "approve",
    # Credit (RAI-271). The review is always a draft in the owner's approvals.
    "get_credit_file": "auto",
    "ask_for_shelf_photo": "notify",
    "count_shelf_photo": "auto",
    "prepare_credit_review": "auto",
    "submit_credit_review": "approve",
    "find_credit_candidates": "auto",
    "find_shop": "auto",
    # Proof's screenshot tools (RAI-268 on the harness, RAI-272)
    "check_payment_screenshot": "auto",
    "record_payment_proof": "notify",
    "flag_payment_proof": "approve",
    # Order's delivery receipt (RAI-267 on the harness)
    "get_open_deliveries": "auto",
    "send_delivery_receipt": "notify",
    "read_receipt_reply": "auto",
    "record_delivery_consent": "notify",
    "flag_delivery_to_owner": "notify",
    "ask_receipt_again": "notify",
    # Stock (RAI-270 on the harness)
    "get_stock_estimates": "auto",
    "ask_stock_count": "notify",
    "read_stock_answer": "auto",
    "record_stock_count": "notify",
    "draft_restock": "approve",
    "revise_restock": "approve",
}

# Tools whose intent only the owner may accept: no table edit or amount rule
# lowers them below approve.
ALWAYS_APPROVE: frozenset[str] = frozenset({"submit_credit_review", "flag_payment_proof",
                                            "draft_restock", "revise_restock"})


class RiskPolicy(Protocol):
    def base_tier(self, tool: str) -> Tier: ...

    def tier(self, tenant_id: str, tool: str, payload: Mapping[str, Any]) -> Tier: ...


@dataclass(frozen=True)
class OwnerLimits:
    """The owner's own limits (Go keeps them per distributor)."""

    # Reminders and plans at or under this total go out on notify; above it
    # they need the owner's approval. N50,000 by default.
    auto_limit_kobo: int = 5_000_000


@dataclass
class GoPolicy:
    tiers: dict[str, Tier] = field(default_factory=lambda: dict(BASE_TIERS))
    limits: dict[str, OwnerLimits] = field(default_factory=dict)
    version: str = "policy-2026-10-27"

    def base_tier(self, tool: str) -> Tier:
        if tool in NEVER_TOOLS:
            return "never"
        return self.tiers.get(tool, "never")

    def tier(self, tenant_id: str, tool: str, payload: Mapping[str, Any]) -> Tier:
        base = self.base_tier(tool)
        if base == "never":
            return "never"
        if tool in ALWAYS_APPROVE:
            return "approve"
        limit = self.limits.get(tenant_id, OwnerLimits()).auto_limit_kobo
        if tool == "send_reminder":
            if payload.get("tone") == "firm" or int(payload.get("amount_kobo") or 0) > limit:
                return "approve"
        if tool == "propose_reminder_plan" and int(payload.get("total_kobo") or 0) <= limit:
            return "notify"
        if tool == "send_to_user" and payload.get("channel") in ("shop_whatsapp", "rep_whatsapp"):
            return "notify"
        return base
