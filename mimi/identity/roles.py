"""Roles, actions and the one permission matrix.

Everything a person may do is a row in ``MATRIX``. There is no ``if role ==
"owner"`` anywhere outside this file, so adding the deferred ``brand_viewer``
later is a row, and a test (``tests/test_identity.py``) can cover every
(role, action) pair, refusals included.

Two axes are separate on purpose:

* **scope** decides *which* shops/items a caller's ground holds (``scopes``).
  A request outside it answers 404, never 403, so ids cannot be probed.
* **role** decides *what* may be done to something inside that ground, here.

The role always comes from our ``users`` row (``mimi.identity.auth``), never
from a token claim: a token that says ``role: owner`` buys nothing.

Agents are not users and have no row here. A helper's limits are its policy
tier in ``mimi.harness.policy``, not a role.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Role = Literal["owner", "supervisor", "rep", "brand_viewer"]
ROLES: tuple[Role, ...] = ("owner", "supervisor", "rep", "brand_viewer")
# brand_viewer is deferred: no user is created with it in the pilot (section 4).

Action = Literal["view_channel", "close_item", "approve_money", "change_credit_limit",
                 "manage_members", "manage_settings", "undo_write"]
ACTIONS: tuple[Action, ...] = ("view_channel", "close_item", "approve_money",
                               "change_credit_limit", "manage_members", "manage_settings",
                               "undo_write")

# How a (role, action) cell is decided, once the caller's scope is known.
#   "every"             anywhere in the tenant
#   "scope"             anywhere in the caller's ground
#   "own_rep_money"     in the caller's ground; a money subject only for a rep
#                       the caller supervises (the approval is recorded with
#                       the decider, the decision and the evidence)
#   "own_non_money"     only a non-money subject assigned to the caller
#   "never"             refused, whatever the scope
Rule = Literal["every", "scope", "own_rep_money", "own_non_money", "never"]

MATRIX: dict[str, dict[str, Rule]] = {
    "owner": {
        "view_channel": "every",
        "close_item": "every",
        "approve_money": "every",
        "change_credit_limit": "every",
        "manage_members": "every",
        "manage_settings": "every",
        "undo_write": "every",
    },
    "supervisor": {
        "view_channel": "scope",
        "close_item": "own_rep_money",
        "approve_money": "own_rep_money",
        # The contract is explicit: a limit is the owner's call, no row for
        # anybody else, and a supervisor approving a limit request is refused
        # the same way as typing one in.
        "change_credit_limit": "never",
        "manage_members": "never",
        # The rulebook, the routines' switches, the connectors and the browser
        # tasks are the distributor's own settings. A supervisor works the
        # list; they do not rewire the machine.
        "manage_settings": "never",
        # Undoing a recorded write (a payment pushed to the books, a limit
        # change) reaches money that already moved: the owner's alone.
        "undo_write": "never",
    },
    "rep": {
        "view_channel": "scope",
        # A rep acts and comments; closing a money item is never theirs.
        "close_item": "own_non_money",
        "approve_money": "never",
        "change_credit_limit": "never",
        "manage_members": "never",
        "manage_settings": "never",
        "undo_write": "never",
    },
    "brand_viewer": {
        # Deferred, and it would see aggregates only: no channel, no item, no
        # money, no member. Adding a real capability is a row here.
        "view_channel": "never",
        "close_item": "never",
        "approve_money": "never",
        "change_credit_limit": "never",
        "manage_members": "never",
        "manage_settings": "never",
        "undo_write": "never",
    },
}

DENIED_ROLES = ("", "channel", "agent", "shop")


@dataclass(frozen=True)
class Principal:
    """The caller, as one object, whichever way they were authenticated."""

    tenant_id: str
    role: str
    user_id: str = ""  # "" for a tenant key or a service call, never a guess
    name: str = ""
    email: str = ""
    phone: str = ""
    status: str = "active"
    # "dev-actor" (X-Actor-Id), "stytch" (a verified session) or "key" (a
    # bearer key with no person attached). Only diagnostics and /v1/me read it.
    source: str = "key"
    # The caller's ground (``mimi.identity.scopes.Scope``). Typed loosely to
    # keep this module free of the store's imports.
    scope: object = None

    @property
    def is_user(self) -> bool:
        return bool(self.user_id)


@dataclass(frozen=True)
class Subject:
    """What an action would touch. ``money`` means it can move or change money."""

    money: bool = False
    area: str = ""
    rep_id: str = ""
    assigned_user_id: str = ""


def _scope_of(principal: Principal) -> object:
    scope = principal.scope
    if scope is None:
        from mimi.identity.scopes import NONE

        return NONE
    return scope


def _in_scope(principal: Principal, subject: Subject) -> bool:
    covers = getattr(_scope_of(principal), "covers_shop", None)
    if covers is None:  # a scope-like object we do not understand: refuse
        return False
    return bool(covers(subject.area, subject.rep_id))


def decide(principal: Principal, action: str, subject: Subject | None = None) -> bool:
    """The one decision function. Every ``can_*`` below is a thin name for it."""
    role = (principal.role or "").strip().lower()
    rule = MATRIX.get(role, {}).get(action, "never")
    subject = subject or Subject()
    if rule == "never":
        return False
    if rule == "every":
        return True
    if rule == "own_non_money":
        return (not subject.money and bool(subject.assigned_user_id)
                and subject.assigned_user_id == principal.user_id)
    # "scope" and "own_rep_money" both start with the caller's ground.
    if not _in_scope(principal, subject):
        return False
    if rule == "scope":
        return True
    if rule == "own_rep_money":
        if not subject.money:
            return True
        covers_rep = getattr(_scope_of(principal), "covers_rep", None)
        return bool(subject.rep_id) and covers_rep is not None and bool(
            covers_rep(subject.rep_id))
    return False


def can_view_channel(principal: Principal, subject: Subject | None = None) -> bool:
    """May the caller see this channel and what is in it."""
    return decide(principal, "view_channel", subject)


def can_close_item(principal: Principal, subject: Subject | None = None) -> bool:
    """May the caller close this item (state ``closed``) with a reason.

    A money item is closable by an owner, or by a supervisor for one of their
    own reps. Never by a rep. Agents are not principals here at all.
    """
    return decide(principal, "close_item", subject)


def can_approve_money(principal: Principal, subject: Subject | None = None) -> bool:
    """May the caller approve a money item (a payment, a limit suggestion).

    The decision must be recorded with the decider, the decision and the
    evidence; ``mimi.api.app`` passes ``decided_by`` from the caller.
    """
    return decide(principal, "approve_money", subject)


def can_change_credit_limit(principal: Principal) -> bool:
    """A limit is the owner's call. No other role, ever."""
    return decide(principal, "change_credit_limit")


def can_manage_members(principal: Principal) -> bool:
    """Inviting, disabling and re-roling people is the owner's."""
    return decide(principal, "manage_members")


def can_manage_settings(principal: Principal) -> bool:
    """The rulebook, routine switches, connectors and browser tasks.

    Not one of the five actions the contract names, but the seam makes it
    necessary: once a supervisor can hold a console key, every route that used
    to be "owner key only" needs a role decision instead, and this is it."""
    return decide(principal, "manage_settings")


def can_undo_write(principal: Principal) -> bool:
    """Undo a recorded write: a payment already pushed to the books, a limit
    change already made. It reaches money that moved, so it is the owner's."""
    return decide(principal, "undo_write")


# --------------------------------------------------------------- subjects
# What counts as "money" for a decision, in words. Conservative on purpose: a
# false positive asks the owner instead of a rep, which is the safe way round.
MONEY_WORDS: frozenset[str] = frozenset({
    "payment", "pay_in", "payout", "credit", "limit", "refund", "transfer", "settle",
    "settlement", "remit", "debit", "write_off", "writeoff", "discount", "bank_detail",
    "allowance", "invoice_amount", "reconcile", "zoho_payment",
})
# Changing a limit, whether typed in or accepted from a suggestion.
CREDIT_LIMIT_WORDS: frozenset[str] = frozenset({
    "credit_limit", "set_credit_limit", "raise_credit_limit", "change_credit_limit",
    "credit_over_limit", "approve_credit_request", "submit_credit_review",
})


def is_money(*labels: str) -> bool:
    """True when these labels (a tool name, an intent kind) name money."""
    text = " ".join(x for x in labels if x).lower()
    return any(word in text for word in MONEY_WORDS)


def is_credit_limit(*labels: str) -> bool:
    """True when this is a credit-limit change dressed as an approval."""
    text = " ".join(x for x in labels if x).lower()
    return any(word in text for word in CREDIT_LIMIT_WORDS)


def in_scope(principal: Principal, subject: Subject) -> bool:
    """Is this subject inside the caller's ground? False for a bare key that is
    not an owner, and for every out-of-territory id (the API answers 404)."""
    return _in_scope(principal, subject)
