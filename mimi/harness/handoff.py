"""Helper handoffs: the typed records the console shows around every routed answer.

Mimi is the Chief of Staff: every message lands on her, she hands it to one
helper, and the helper answers. This module is the contract for that, as
plain typed records (``docs/HANDOFFS.md``; field names are stable, the
console reads them):

* **Handoff** (``handoff``): from ``mimi`` to a helper, a one-line ack written
  by code ("Asking Collector"), how the helper was picked (``via``), the
  hop (0, or 1 after a hand-back), the steps the helper ran with their
  timings, and a status (``working``, ``needs_you``, ``done``,
  ``handed_back``, ``failed``).
* **Handback** (``handback``): a helper that could not answer (it pointed
  the person somewhere else, it has no tools for the ask, or it failed on a
  deflection) returns the message to Mimi, who re-routes it once to the
  right helper. Max one hop; after that Mimi answers herself or asks one
  clear question. Never a dead end.
* **Card** (``card``): a structured answer beside one short sentence. A
  ``shop_row`` (one shop: owed, overdue, days late, credit limit) or a
  ``list`` (orders, low stock, debtors). Every figure on a card is read from
  the book by code (``mimi.api.cards``), never from model text.
* **Chip** (``chips``): two or three suggested next steps, each a label and
  an intent the console sends back as a new turn. Safe tier only: a chip
  never moves money or approves anything; it only asks Mimi something, and
  that turn is routed and guarded like any other.
* **Assignment**: the owner may name the helper (``@Collector ...`` at the
  start of a message, or ``assign_to`` on ``POST /v1/turns``). The router
  honours it for the owner only, still inside the owner's own skills, still
  under every policy tier. A shop's @mention is never honoured.

Pure code: no model, no store, no web framework.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mimi.harness import lexicon
from mimi.harness.claims import deflections
from mimi.harness.skills import BY_KEY, HELPER_TITLE, HELPERS, MONEY_SKILLS, allowed_for

CONTRACT_VERSION = "handoffs-2026-10-30.1"

TaskStatus = Literal["working", "needs_you", "done", "handed_back", "failed"]
Via = Literal["rule", "lexical", "fast", "model", "mention", "assign", "handback", "binding"]
HandbackCode = Literal["deflected", "no_tools", "no_helper", "failed"]
CardKind = Literal["shop_row", "list"]
ActionKind = Literal["remind", "open_shop", "open_order", "open_approvals", "ask"]
IntentKind = Literal["turn", "open"]

# Max re-routes after a hand-back: one hop, then Mimi answers or asks.
MAX_HOPS = 1
# A card's sentence is one sentence, at most this long.
MAX_SENTENCE = 220


class _Rec(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HandoffStep(_Rec):
    """One step of the helper's work, written by code from the run's events."""

    name: str  # "route", a tool name ("get_balance"), "model", "reply"
    kind: Literal["route", "tool", "model", "reply", "handback", "code"]
    status: Literal["ok", "refused", "held", "failed"] = "ok"
    ms: float = 0.0
    at: str = ""


class Handoff(_Rec):
    handoff_id: str
    contract: str = CONTRACT_VERSION
    from_helper: Literal["mimi"] = "mimi"
    to_helper: str
    to_title: str
    skill: str
    reason: str  # the one-line ack: "Asking Collector"
    via: Via
    hop: int = 0
    status: TaskStatus
    steps: list[HandoffStep] = Field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""


class Handback(_Rec):
    """A helper returned the message to Mimi instead of answering it."""

    from_helper: str
    from_skill: str
    code: HandbackCode
    reason: str  # code-written, one line
    to_helper: str = ""  # where Mimi re-routed it ("" when Mimi answered herself)
    to_skill: str = ""
    resolved: Literal["rerouted", "mimi_answered", "asked_owner", "asked_sender"]
    spans: list[str] = Field(default_factory=list)  # the deflecting words, if any
    first_run_id: str = ""


class ChipIntent(_Rec):
    """What the console sends when the chip is tapped.

    ``turn``: POST /v1/turns with ``text`` (and ``assign_to`` when set).
    ``open``: navigate to ``target`` (a shop, an order, the approvals list);
    no turn, nothing changes."""

    kind: IntentKind
    text: str = ""
    assign_to: str = ""
    target: str = ""  # "shop:<shop_id>", "approvals", "order:<order_id>"


class Chip(_Rec):
    label: str
    intent: ChipIntent
    tier: Literal["safe"] = "safe"


class CardAction(_Rec):
    action: ActionKind
    label: str
    intent: ChipIntent
    tier: Literal["safe"] = "safe"


class ShopRow(_Rec):
    shop_id: str
    name: str
    owed_kobo: int
    owed_naira: str
    overdue_kobo: int
    overdue_naira: str
    days_late: int
    credit_limit_kobo: int
    credit_limit_naira: str
    open_invoices: int = 0


class ListItem(_Rec):
    item_id: str
    label: str
    qty: int | None = None
    unit: str = ""
    status: str = ""
    amount_kobo: int | None = None
    amount_naira: str = ""
    shop_id: str = ""


class Card(_Rec):
    kind: CardKind
    title: str
    skill: str
    shop_row: ShopRow | None = None
    items: list[ListItem] = Field(default_factory=list)
    total: int = 0  # items in the book behind the list (it may show fewer)
    # The sum of every item's amount behind the list (not only those shown), in
    # kobo, when the items carry amounts; summed in code.
    total_kobo: int | None = None
    total_naira: str = ""
    empty_text: str = ""
    actions: list[CardAction] = Field(default_factory=list)
    as_of: str = ""
    source: Literal["book"] = "book"


# ------------------------------------------------------------------ acks


def title(helper: str) -> str:
    return HELPER_TITLE.get(helper, helper.title())


def ack(helper: str, *, hop: int = 0, from_helper: str = "") -> str:
    """The one-line reason the console shows before the helper's answer."""
    if helper == "mimi":
        return "Mimi is answering"
    if hop and from_helper:
        return f"{title(from_helper)} handed it back; asking {title(helper)}"
    return f"Asking {title(helper)}"


def via_of(layer: str, rule: str) -> Via:
    if rule.startswith("owner_assign"):
        return "assign"
    if rule.startswith("owner_mention"):
        return "mention"
    if rule.startswith("handback"):
        return "handback"
    if rule.startswith(("pending_", "steer")):
        return "binding"
    if layer in ("rule", "lexical", "fast", "model"):
        return layer  # type: ignore[return-value]
    return "rule"


def task_status(state: str, status: str, *, pending_approvals: int = 0) -> TaskStatus:
    """A turn's state and status as the helper's task status."""
    if state in ("queued", "leased"):
        return "working"
    if state == "failed" or status in ("failed",):
        return "failed"
    if state == "parked" or pending_approvals > 0 or status in (
            "parked_for_approval", "asked_to_clarify", "parked_budget", "steer_which_owner"):
        return "needs_you"
    if status.startswith("handed_back"):
        return "handed_back"
    return "done"


# ------------------------------------------------------------------ steps


_STEP_TYPES = {"tool_result": "tool", "tool_refused": "tool", "model_called": "model",
               "claim_blocked": "tool", "draft_proposed": "reply"}


def steps_from_events(events: Iterable[Any], *, route_ms: float = 0.0,
                      route_at: str = "", route_name: str = "route") -> list[HandoffStep]:
    """The helper's steps for the console: the route, each tool and model call,
    each reply, with timings. Written by code from the run's events; never the
    model's private text."""
    out = [HandoffStep(name=route_name, kind="route", ms=round(route_ms, 3), at=route_at)]
    for e in events:
        typ = getattr(e, "type", "")
        if typ not in _STEP_TYPES:
            continue
        kind = _STEP_TYPES[typ]
        status: Literal["ok", "refused", "held", "failed"] = "ok"
        if typ == "tool_refused":
            status = "refused"
        elif typ == "claim_blocked":
            status = "held"
        name = getattr(e, "tool", "") or ("model" if kind == "model" else kind)
        at: Any = getattr(e, "at", None)
        stamp = at.isoformat() if at is not None and hasattr(at, "isoformat") else str(at or "")
        out.append(HandoffStep(name=name, kind=kind, status=status,  # type: ignore[arg-type]
                               ms=round(float(getattr(e, "duration_ms", 0.0) or 0.0), 3),
                               at=stamp))
    return out


# -------------------------------------------------------------- hand-back


def deflected(events: Iterable[Any]) -> list[str]:
    """Deflecting words the claims gate held in this run (empty when none)."""
    spans: list[str] = []
    for e in events:
        if getattr(e, "type", "") != "claim_blocked":
            continue
        probs = (getattr(e, "data", None) or {}).get("problems") or {}
        spans.extend(str(x) for x in probs.get("deflection") or ())
    return spans


def deflecting_texts(texts: Iterable[str]) -> list[str]:
    spans: list[str] = []
    for t in texts:
        spans.extend(deflections(t))
    return spans


# Words in a deflection or a message that name the helper it belongs to.
_HELPER_HINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("order", re.compile(r"\b(?:orders?|ordering|restock|deliver(?:y|ies)|in stock|available)\b",
                         re.I)),
    ("stock", re.compile(r"\b(?:stock|shelf|running low|run out)\b", re.I)),
    ("collector", re.compile(r"\b(?:owe[sd]?|owing|debtors?|balance|outstanding|collect)\b",
                             re.I)),
    ("credit", re.compile(r"\b(?:credit|limit)\b", re.I)),
    ("brand", re.compile(r"\b(?:promo|promotion|discount|campaign|poster)\b", re.I)),
)


def reroute(text: str, sender: str, *, exclude_helper: str, spans: Sequence[str] = (),
            owner_rule: str = "") -> str:
    """The skill Mimi hands a returned message to, or "" when none is clear.

    Never the helper that handed it back, only a skill this sender may reach,
    and never a money skill on a hint alone: the sender's own words must name
    it by phrase rule (score 3 or more). Order: an owner rule the router
    already knows (``owner_rule``), then the phrase rules without the
    returning helper's skills, then the words of the deflection itself
    ("the orders helper will help") for a non-money skill."""
    allowed = {s.key for s in allowed_for(sender)}
    if owner_rule and owner_rule in BY_KEY and owner_rule.split("/")[0] != exclude_helper:
        return owner_rule
    pool = frozenset(k for k in allowed if k.split("/")[0] != exclude_helper)
    lx = lexicon.score(lexicon.without_injection(text) if sender != "owner" else text, pool)
    if lx.top and lx.top_score >= lexicon.STRONG and lx.top_score - lx.second_score >= 1:
        return lx.top
    for words in (" ".join(spans), text):
        for helper, rx in _HELPER_HINTS:
            if helper == exclude_helper or not rx.search(words):
                continue
            cands = [k for k in sorted(pool) if k.split("/")[0] == helper
                     and k not in MONEY_SKILLS]
            if cands:
                return _best(cands, sender)
    return ""


def _best(cands: Sequence[str], sender: str) -> str:
    # The skill whose senders list this sender first; owner order skills
    # first look at what is open (``order/open-orders``).
    pref = ("order/open-orders", "order/order-intake", "stock/low-stock-report",
            "stock/stock-count", "brand/promo-info")
    for p in pref:
        if p in cands:
            return p
    return cands[0]


# ------------------------------------------------------------------- text


_SENT_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(N₦])")


def first_sentence(text: str, limit: int = MAX_SENTENCE) -> str:
    """The first sentence of ``text`` (a card carries the rest)."""
    t = " ".join(text.split())
    if not t:
        return ""
    head = _SENT_END.split(t, maxsplit=1)[0]
    head = head.split("\n", 1)[0].strip()
    if len(head) > limit:
        cut = head[:limit].rsplit(" ", 1)[0].rstrip(",;:-")
        head = cut + "."
    return head


def sentence_count(text: str) -> int:
    t = " ".join(text.split())
    return len([p for p in _SENT_END.split(t) if p.strip()]) if t else 0


# ------------------------------------------------------------ assignment


_LEAD_MENTION = re.compile(r"^\s*@(order|orders|collector|credit|stock|brand|proof|mimi)\b[\s,:-]*",
                           re.I)


def leading_mention(text: str) -> str:
    """The helper an owner names at the start of a message ("@Collector ..."), or ""."""
    m = _LEAD_MENTION.match(text or "")
    if not m:
        return ""
    h = m.group(1).lower()
    return "order" if h == "orders" else h


def assignable(helper: str) -> bool:
    return helper in HELPERS


# ------------------------------------------------------------------ chips


def _turn(label: str, text: str, assign_to: str = "") -> Chip:
    return Chip(label=label, intent=ChipIntent(kind="turn", text=text, assign_to=assign_to))


def _open(label: str, target: str) -> Chip:
    return Chip(label=label, intent=ChipIntent(kind="open", target=target))


def chips_for(skill: str, sender: str, card: Card | None = None) -> list[Chip]:
    """Two or three safe next steps after an answer (owner console only).

    Each is a question back to Mimi or a place to open; none approves,
    sends money or changes a record by itself. A reminder chip asks
    Collector to draft one, which goes through Collector's own tiers."""
    if sender != "owner":
        return []
    row = card.shop_row if card is not None else None
    out: list[Chip] = []
    if skill.startswith("collector/") and row is not None:
        out = [_turn(f"Remind {row.name}", f"Send {row.shop_id} a soft reminder about the "
                     "overdue balance", "collector"),
               _open(f"Open {row.name}", f"shop:{row.shop_id}"),
               _turn("Who else owes?", "Who else owes me money?", "collector")]
    elif skill.startswith("collector/"):
        out = [_turn("Who owes the most?", "Who is the highest debtor?", "collector"),
               _turn("Remind overdue shops", "Draft soft reminders for the overdue shops",
                     "collector"),
               _open("Open approvals", "approvals")]
    elif skill == "order/open-orders":
        out = [_open("Open approvals", "approvals"),
               _turn("Who is running low?", "Which shops are running low on stock?", "stock"),
               _turn("Deliveries today", "Which deliveries are still open?", "order")]
    elif skill.startswith("stock/"):
        out = [_turn("Any new orders?", "Any orders waiting now?", "order"),
               _open("Open approvals", "approvals")]
    elif skill.startswith("credit/"):
        out = [_open("Open approvals", "approvals"),
               _turn("Who owes the most?", "Who is the highest debtor?", "collector")]
    elif skill.startswith("proof/"):
        out = [_open("Open approvals", "approvals"),
               _turn("Who owes the most?", "Who is the highest debtor?", "collector")]
    elif skill.startswith("brand/"):
        out = [_turn("Any new orders?", "Any orders waiting now?", "order"),
               _open("Open approvals", "approvals")]
    else:
        out = [_turn("Who owes the most?", "Who is the highest debtor?", "collector"),
               _turn("Any new orders?", "Any orders waiting now?", "order"),
               _open("Open approvals", "approvals")]
    return out[:3]


# Words a chip, a card action or its intent may never carry: a tap is a
# question, never a money move or a decision.
_UNSAFE = re.compile(r"\b(?:approve|approved|transfer|pay(?:out)?|refund|wire|settle|remit|"
                     r"debit|send money|credit limit to|raise (?:the )?limit|disburse)\b", re.I)


def chip_is_safe(chip: Chip | CardAction) -> bool:
    i = chip.intent
    words = " ".join((chip.label, i.text, i.target))
    return chip.tier == "safe" and not _UNSAFE.search(words) and (
        not i.assign_to or i.assign_to in HELPERS)


def safe_chips(chips: Iterable[Chip]) -> list[Chip]:
    return [c for c in chips if chip_is_safe(c)][:3]


def dump(x: BaseModel | None) -> dict[str, Any] | None:
    return x.model_dump(mode="json") if x is not None else None


def dump_list(xs: Iterable[BaseModel]) -> list[dict[str, Any]]:
    return [x.model_dump(mode="json") for x in xs]


def load_handoff(raw: Mapping[str, Any] | None) -> Handoff | None:
    return Handoff.model_validate(raw) if raw else None
