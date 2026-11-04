"""Risk tiers and the per-distributor rulebook, read from policy, never the model.

``RiskPolicy`` is Go's policy endpoint. ``GoPolicy`` is its stand-in with the
same fixed table and amount rules, so tests and the sim behave as production
will. Two rules hold for every helper at every trust level:

* moving money and changing bank details are tier ``never`` (RAI-288);
* a tool Go has no policy row for is ``never`` (fail closed).

The amount rules compare integer kobo computed by code (the tool's own
output), never a figure the model wrote.

The rulebook (RAI-323)
----------------------
Every consequential step a helper takes has an *action type*
(``collector.send_reminder``, ``order.confirm_reorder``...). Each distributor
picks one behaviour per action type:

* ``act``: Mimi goes ahead within code's limits (the amount and tone rules
  above can still send it to the owner).
* ``act_if_preapproved``: Mimi goes ahead only when code finds a standing
  pre-approval for this step (``context["preapproved"]``, set by code, never
  the model); otherwise it asks.
* ``ask``: the step waits in the owner's approvals.
* ``hand_off``: the step becomes an approval the owner completes by hand;
  Mimi never carries it out.

``decide(tenant, action_type, context)`` is the single seam. Built-in stops
(``BUILTIN_STOPS``: moving money, changing bank details, setting a credit
limit, asking for a PIN or OTP, changing a password or key, deleting shop
data) come first and answer ``never`` whatever a rule, the API or a message
says. A few action types have a floor (a brand email is at least ``ask``).
An action type with no catalogue entry is ``ask``, never ``act``. The default
rules reproduce the behaviour before the rulebook exactly, so a distributor
with no rows of their own sees no change.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

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
    # Every helper may hand work to another (MIMI_HELPER_DELEGATION); the tool
    # is in no toolset until the switch is on.
    #
    # POLICY_VERSION is NOT bumped for this row yet, deliberately: the row
    # changes no helper's toolset or recorded request while the switch is off,
    # and a bump would make every recorded loop answer miss as stale (RAI-309)
    # with no way to re-record here (no key). The bump travels with the
    # deployment turn-on, in the same change as `--live` re-records.
    "delegate_task": "auto",
    "notify_rep": "auto",
    "notify_owner": "auto",
    # The owner raises the day's model allowance when the daily cap parks
    # helpers (RAI-272). Mimi's own spend, never the book's money.
    "raise_daily_allowance": "approve",
    # Brand's Promo Check (RAI-272): reads promos; brand emails stay drafts.
    "get_brand_promos": "auto",
    "draft_brand_email": "approve",
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
    # Weekly check-in (RAI-276 on the harness; on the stock helper's toolset)
    "send_weekly_checkin": "notify",
    "read_checkin_reply": "auto",
    "record_supplier_lines": "notify",
    "submit_checkin_count": "notify",
    # Order intake (RAI-302). Every order waits for the owner or rep.
    "read_order_photo": "auto",
    "price_order": "auto",
    "ask_order_question": "notify",
    "submit_order": "approve",
    "revise_order": "approve",
    # Skills (RAI-314, MIMI_SKILLS only): read-only, they compute or hand back
    # vetted text from the repo; nothing they return is an intent.
    "read_skill": "auto",
    "followup_hours": "auto",
    "say_date": "auto",
    "parse_bank_alert": "auto",
}

# Tools whose intent only the owner may accept: no table edit or amount rule
# lowers them below approve.
ALWAYS_APPROVE: frozenset[str] = frozenset({"submit_credit_review", "flag_payment_proof",
                                            "draft_restock", "revise_restock",
                                            "draft_brand_email", "raise_daily_allowance",
                                            "submit_order", "revise_order"})


def go_allows(tool: str, kind: str = "", tiers: Mapping[str, Tier] | None = None) -> bool:
    """Go's own last line, whatever tier a record arrives with: the tool must be on
    the policy allowlist (a row in ``tiers``, ``BASE_TIERS`` by default), must not be
    a ``never`` tool, and neither its name nor its kind may carry a money word."""
    from mimi.runtime.outbox import MONEY_VERBS

    table = BASE_TIERS if tiers is None else tiers
    name = tool or kind
    if not name or name in NEVER_TOOLS or table.get(name, "never") == "never":
        return False
    low = f"{tool} {kind}".lower()
    return not any(v in low for v in MONEY_VERBS)


# ================================================================ the rulebook

log = logging.getLogger(__name__)

Behaviour = Literal["act", "act_if_preapproved", "ask", "hand_off", "never"]
# What an owner may pick, strictest last (``never`` is for built-in stops only).
BEHAVIOURS: tuple[Behaviour, ...] = ("act", "act_if_preapproved", "ask", "hand_off")
_STRICTNESS: dict[str, int] = {"act": 0, "act_if_preapproved": 1, "ask": 2, "hand_off": 3,
                               "never": 4}
Source = Literal["built_in", "default", "owner", "fail_closed", "unknown"]


@dataclass(frozen=True)
class ActionType:
    """One consequential step, as the owner sees it in Settings."""

    key: str
    helper: str
    label: str  # plain words for the console list
    default: Behaviour
    # The loosest behaviour an owner may pick (a brand email is at least ``ask``).
    floor: Behaviour = "act"
    tools: tuple[str, ...] = ()


def _a(key: str, helper: str, label: str, default: Behaviour, tools: tuple[str, ...],
       floor: Behaviour = "act") -> ActionType:
    return ActionType(key, helper, label, default, floor, tools)


# Built-in stops: frozen in code, shown locked, no rule or API can change them.
BUILTIN_STOPS: Mapping[str, ActionType] = {
    a.key: a for a in (
        _a("payment.move_money", "all", "Move money (pay, send, refund)", "never",
           ("transfer_money", "send_money", "pay_out", "refund_payment"), "never"),
        _a("bank.change_details", "all", "Change bank details", "never",
           ("change_bank_details", "update_bank_account"), "never"),
        _a("credit.set_limit", "credit", "Set or change a credit limit", "never",
           ("set_credit_limit", "change_credit_limit", "raise_credit_limit",
            "approve_credit_request"), "never"),
        _a("debt.change_terms", "collector",
           "Write off debt, change an amount, extend a due date or give a discount", "never",
           ("write_off_debt", "change_invoice_amount", "extend_due_date", "give_discount"),
           "never"),
        _a("account.ask_pin_or_otp", "all", "Ask anyone for a PIN or OTP", "never",
           ("request_pin_or_otp",), "never"),
        _a("account.change_password_or_key", "all", "Change a password or key", "never",
           ("change_password", "reset_password", "rotate_api_key", "change_api_key"), "never"),
        _a("shop.delete_data", "all", "Delete a shop's data", "never",
           ("delete_shop", "delete_shop_data", "erase_shop_data"), "never"),
    )
}

# Consequential steps the owner can set. Defaults equal today's tiers.
ACTION_TYPES: Mapping[str, ActionType] = {
    a.key: a for a in (
        _a("collector.send_reminder", "collector", "Send a payment reminder to a shop", "act",
           ("send_reminder",)),
        _a("collector.plan_reminders", "collector", "Plan the day's reminders", "act",
           ("propose_reminder_plan",)),
        _a("collector.send_receipt", "collector", "Send a shop a payment receipt", "act",
           ("send_receipt",)),
        _a("collector.create_payment_account", "collector",
           "Give a shop an exact-amount account to pay into", "act",
           ("create_exact_amount_account",)),
        _a("collector.notify_rep", "collector", "Tell a sales rep about a shop", "act",
           ("notify_rep",)),
        _a("shop.send_message", "all", "Send a shop a message", "act", ()),
        _a("shop.ask_question", "all", "Ask a shop a question", "act", ()),
        _a("rep.send_message", "all", "Send a sales rep a message", "act", ()),
        _a("order.confirm_reorder", "order", "Confirm a shop's order", "act_if_preapproved",
           ("submit_order", "revise_order"), "act_if_preapproved"),
        _a("order.ask_question", "order", "Ask a shop about its order", "act",
           ("ask_order_question",)),
        _a("order.send_delivery_receipt", "order", "Send a shop a delivery receipt", "act",
           ("send_delivery_receipt",)),
        _a("order.record_delivery_consent", "order", "Record a shop's delivery answer", "act",
           ("record_delivery_consent",)),
        _a("order.ask_receipt_again", "order", "Ask a shop to answer a receipt again", "act",
           ("ask_receipt_again",)),
        _a("order.flag_delivery", "order", "Flag a delivery problem to you", "act",
           ("flag_delivery_to_owner",)),
        # The weekly check-in (RAI-276) is the same ask and the same count.
        _a("stock.ask_count", "stock", "Ask a shop to count its stock", "act",
           ("ask_stock_count", "send_weekly_checkin")),
        _a("stock.record_count", "stock", "Record a shop's stock count", "act",
           ("record_stock_count", "submit_checkin_count")),
        _a("stock.record_supplier_lines", "stock",
           "Record stock a shop bought from another supplier", "act",
           ("record_supplier_lines",)),
        _a("stock.draft_restock", "stock", "Draft a restock order", "act_if_preapproved",
           ("draft_restock", "revise_restock"), "act_if_preapproved"),
        _a("credit.ask_shelf_photo", "credit", "Ask a shop for a shelf photo", "act",
           ("ask_for_shelf_photo",)),
        _a("credit.suggest_limit", "credit", "Suggest a credit limit for you to decide", "ask",
           ("submit_credit_review",), "ask"),
        _a("proof.record_payment", "proof", "Record a checked payment proof", "act",
           ("record_payment_proof",)),
        _a("proof.flag_payment", "proof", "Flag a payment proof that looks wrong", "ask",
           ("flag_payment_proof",), "ask"),
        _a("brand.send_email", "brand", "Email a brand or bank", "ask",
           ("draft_brand_email",), "ask"),
        _a("plan.raise_allowance", "mimi", "Raise today's model allowance", "ask",
           ("raise_daily_allowance",), "ask"),
    )
}

# Steps inside Mimi's own working (her notes, the owner's own chat, timers,
# handing work between helpers). They still pass through ``decide`` and are
# always ``act``; they are not in the owner's list because nobody outside sees
# them as an action.
INTERNAL_ACTIONS: Mapping[str, ActionType] = {
    a.key: a for a in (
        _a("internal.plan", "all", "Update the work plan", "act", ("update_plan",)),
        _a("internal.follow_up", "all", "Set a follow-up timer", "act", ("set_followup",)),
        _a("internal.ask_helper", "all", "Hand work to another helper", "act", ("ask_helper",)),
        _a("internal.delegate", "all", "Hand work to another helper", "act",
           ("delegate_task",)),
        _a("internal.notify_owner", "all", "Tell you something", "act", ("notify_owner",)),
        _a("internal.owner_message", "all", "Write to you", "act", ()),
        _a("internal.skill", "all", "Read a skill or compute", "act",
           ("read_skill", "followup_hours", "say_date", "parse_bank_alert")),
    )
}

# Read tools: they change nothing, so they are not actions (``internal.read``).
# Listed by name so a new write tool can never slip in as a read: a test
# checks each one is registered ``kind="read"``.
READ_TOOLS: frozenset[str] = frozenset({
    "get_balance", "get_invoices", "get_payment_history", "get_account_number",
    "check_payment", "get_brand_promos", "check_bank_feed", "open_bank_portal",
    "get_credit_file", "count_shelf_photo", "prepare_credit_review",
    "find_credit_candidates", "find_shop", "check_payment_screenshot",
    "get_open_deliveries", "read_receipt_reply", "get_stock_estimates",
    "read_stock_answer", "read_order_photo", "price_order", "read_checkin_reply",
})
INTERNAL_ACTIONS = {**INTERNAL_ACTIONS, "internal.read": _a(
    "internal.read", "all", "Read the book", "act", tuple(sorted(READ_TOOLS)))}

_BY_TOOL: dict[str, str] = {
    t: a.key for table in (ACTION_TYPES, INTERNAL_ACTIONS, BUILTIN_STOPS)
    for a in table.values() for t in a.tools
}
STOP_TOOLS: frozenset[str] = frozenset(
    t for a in BUILTIN_STOPS.values() for t in a.tools) | NEVER_TOOLS


def action_type_for(tool: str, payload: Mapping[str, Any] | None = None) -> str:
    """The action type of one tool call (``unknown.<tool>`` when none: ``ask``)."""
    p = payload or {}
    if tool in NEVER_TOOLS and tool not in _BY_TOOL:
        return "payment.move_money"
    if tool == "send_to_user":
        ch = str(p.get("channel") or "")
        return {"shop_whatsapp": "shop.send_message",
                "rep_whatsapp": "rep.send_message"}.get(ch, "internal.owner_message")
    if tool == "ask_question":
        return "shop.ask_question" if p.get("to") == "shop" else "internal.owner_message"
    return _BY_TOOL.get(tool, f"unknown.{tool}")


def catalogue(action_type: str) -> ActionType | None:
    return (BUILTIN_STOPS.get(action_type) or ACTION_TYPES.get(action_type)
            or INTERNAL_ACTIONS.get(action_type))


def allowed_behaviours(action_type: str) -> tuple[Behaviour, ...]:
    """What the owner may set this action type to (empty: locked)."""
    a = ACTION_TYPES.get(action_type)
    if a is None:
        return ()
    return tuple(b for b in BEHAVIOURS if _STRICTNESS[b] >= _STRICTNESS[a.floor])


class PolicyLocked(ValueError):
    """A rule change the rulebook refuses (a built-in stop, an internal step, a
    behaviour below the floor, or an unknown action type)."""


def check_rule(action_type: str, behaviour: str) -> None:
    if action_type in BUILTIN_STOPS:
        raise PolicyLocked(f"{action_type} is a built-in stop: Mimi never does it")
    if action_type in INTERNAL_ACTIONS:
        raise PolicyLocked(f"{action_type} is part of Mimi's own working, not a rule")
    if action_type not in ACTION_TYPES:
        raise PolicyLocked(f"unknown action type {action_type}")
    if behaviour not in allowed_behaviours(action_type):
        raise PolicyLocked(f"{action_type} cannot be {behaviour}; allowed: "
                           + ", ".join(allowed_behaviours(action_type)))


class RuleStore(Protocol):
    """A distributor's own rows: action type to behaviour. Only the API writes."""

    def rules(self, tenant_id: str, conn: Any = None) -> Mapping[str, str]: ...


@dataclass
class MemoryRules:
    """In-process rule rows (tests, the sim). ``SqlPolicyRules`` is the store's."""

    rows: dict[str, dict[str, str]] = field(default_factory=dict)

    def rules(self, tenant_id: str, conn: Any = None) -> Mapping[str, str]:
        return dict(self.rows.get(tenant_id, {}))

    def set(self, tenant_id: str, action_type: str, behaviour: str) -> None:
        check_rule(action_type, behaviour)
        self.rows.setdefault(tenant_id, {})[action_type] = behaviour


def owner_rules_on(env: Mapping[str, str] | None = None) -> bool:
    """``MIMI_POLICY_RULES=0`` ignores every owner row (the defaults only): the
    rollback switch. On by default; with no rows the defaults are today's."""
    e = os.environ if env is None else env
    return e.get("MIMI_POLICY_RULES", "1").strip() != "0"


@dataclass(frozen=True)
class Decision:
    """What ``decide`` answered for one step, and why."""

    action_type: str
    behaviour: Behaviour  # the rule that applied
    tier: Tier  # what the loop and Go do with it
    source: Source
    locked: bool = False
    reason: str = ""

    @property
    def hand_off(self) -> bool:
        return self.behaviour == "hand_off"

    @property
    def outcome(self) -> str:
        """``act`` | ``ask`` | ``hand_off`` | ``refuse``: what actually happens."""
        if self.tier == "never":
            return "refuse"
        if self.behaviour == "hand_off":
            return "hand_off"
        return "ask" if self.tier == "approve" else "act"


class RiskPolicy(Protocol):
    def base_tier(self, tool: str) -> Tier: ...

    def tier(self, tenant_id: str, tool: str, payload: Mapping[str, Any], *,
             conn: Any = None) -> Tier: ...

    def decide(self, tenant_id: str, action_type: str,
               context: Mapping[str, Any]) -> Decision: ...


@dataclass(frozen=True)
class OwnerLimits:
    """The owner's own limits (Go keeps them per distributor)."""

    # Reminders and plans at or under this total go out on notify; above it
    # they need the owner's approval. N50,000 by default.
    auto_limit_kobo: int = 5_000_000


# Bump on any tier, allowlist or limit change: recorded loop answers are keyed
# by it (RAI-309), so a bump makes every replayed loop call miss as stale.
POLICY_VERSION = "policy-2026-10-27"


@dataclass
class GoPolicy:
    tiers: dict[str, Tier] = field(default_factory=lambda: dict(BASE_TIERS))
    limits: dict[str, OwnerLimits] = field(default_factory=dict)
    version: str = POLICY_VERSION
    # The distributor's own rows (RAI-323). None: the defaults only.
    rules: RuleStore | None = None
    owner_rules: bool = field(default_factory=owner_rules_on)

    def base_tier(self, tool: str) -> Tier:
        if tool in STOP_TOOLS:
            return "never"
        return self.tiers.get(tool, "never")

    def tier(self, tenant_id: str, tool: str, payload: Mapping[str, Any], *,
             conn: Any = None) -> Tier:
        """The tier of one step: always through ``decide`` (RAI-323). ``conn``:
        the caller's open store transaction, so the rules are read inside it."""
        ctx: dict[str, Any] = {"tool": tool, "payload": payload}
        if conn is not None:
            ctx["conn"] = conn
        return self.decide(tenant_id, action_type_for(tool, payload), ctx).tier

    def decide(self, tenant_id: str, action_type: str,
               context: Mapping[str, Any]) -> Decision:
        """act | act_if_preapproved | ask | hand_off for one consequential step.

        ``context`` carries ``tool`` and ``payload`` (the tool's own output,
        figures in kobo from code) and ``preapproved`` (code's word only).
        Order: built-in stops, then the tool's policy row, then the owner's
        rule (or the default), then the amount and tone rules, which can only
        make a step stricter. A rule store that cannot be read fails closed:
        every owner-settable step asks."""
        tool = str(context.get("tool") or "")
        payload = context.get("payload") or {}
        spec_any = catalogue(action_type)
        if not tool and spec_any is not None and spec_any.tools:
            tool = spec_any.tools[0]
        if action_type in BUILTIN_STOPS or tool in STOP_TOOLS:
            key = action_type if action_type in BUILTIN_STOPS else action_type_for(tool)
            return Decision(key, "never", "never", "built_in", locked=True,
                            reason="built-in stop: Mimi never does this")
        code = self._code_tier(tenant_id, tool, payload) if tool else "approve"
        if code == "never":
            return Decision(action_type, "never", "never", "built_in", locked=True,
                            reason="no policy row for this tool (fail closed)")
        if action_type in INTERNAL_ACTIONS:
            return Decision(action_type, "act", code, "default", locked=True,
                            reason="part of Mimi's own working")
        spec = ACTION_TYPES.get(action_type)
        if spec is None:
            return Decision(action_type, "ask", "approve", "unknown",
                            reason="unknown action type: asks the owner")
        behaviour: str = spec.default
        source: Source = "default"
        if self.rules is not None and self.owner_rules:
            try:
                row = self.rules.rules(tenant_id, conn=context.get("conn")).get(action_type)
            except Exception as exc:  # fail closed: the owner may have been stricter
                log.warning("policy rules unreadable: %s", type(exc).__name__)
                return Decision(action_type, "ask", "approve", "fail_closed",
                                reason="the owner's rules could not be read")
            if row is not None and row in allowed_behaviours(action_type):
                behaviour, source = row, "owner"
        if behaviour == "act":
            tier = code
        elif behaviour == "act_if_preapproved":
            # A standing pre-approval stands in for the owner's tap: Go applies
            # it and tells the owner. Without one it waits, as today.
            pre = context.get("preapproved") is True
            tier = ("notify" if code == "approve" else code) if pre else "approve"
        else:
            tier = "approve"
        return Decision(action_type, behaviour, tier, source,  # type: ignore[arg-type]
                        reason=f"{source} rule: {behaviour}")

    def _code_tier(self, tenant_id: str, tool: str, payload: Mapping[str, Any]) -> Tier:
        """Today's tier from the table and the amount and tone rules."""
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


_DEFAULT_POLICY = GoPolicy()


def decide(tenant_id: str, action_type: str, context: Mapping[str, Any], *,
           policy: RiskPolicy | None = None) -> Decision:
    """``policy.decide(tenant, action_type, context)``: the one seam (RAI-323)."""
    return (policy or _DEFAULT_POLICY).decide(tenant_id, action_type, context)
