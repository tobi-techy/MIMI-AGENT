"""Request and response bodies of the HTTP API (the OpenAPI contract).

Money is integer kobo (``*_kobo``) with a display string beside it
(``*_naira``) computed in code. Times are ISO 8601 with their zone.
Phone numbers never leave the API in full: only the last four digits.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mimi.harness.handoff import Card, Chip, Handback, Handoff

RECEIVED_AT = ("When the message was received, with its zone. Defaults to now. A time "
               "without a zone is read as Africa/Lagos (WAT, UTC+1).")

class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MediaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["image", "voice", "document"]
    # Where the channel keeps the bytes (a WhatsApp media id or a storage key).
    # Mimi never stores the media itself.
    ref: str = Field("", max_length=500)
    mime: str = Field("", max_length=100)
    sha256: str = Field("", max_length=64)
    width: int = 0
    height: int = 0
    camera_exif: bool = False
    caption: str = Field("", max_length=4000)
    # A voice note's transcript, when the channel already made one.
    transcript: str = Field("", max_length=8000)
    transcript_lang: str = Field("", max_length=16)


class TurnIn(BaseModel):
    """One inbound message from a shop, a rep or the owner."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(..., min_length=1, max_length=200,
                            description="The channel's own message id (WhatsApp wamid, console "
                                        "id). The same id twice is one turn.")
    tenant_id: str = Field("", max_length=64,
                           description="Optional; must match the bearer key's distributor.")
    source: Literal["shop_message", "owner_message", "rep_message"] = "shop_message"
    phone: str = Field("", max_length=32,
                       description="The sender's number as the channel gave it. Empty for the "
                                   "owner in the console.")
    text: str = Field("", max_length=8000)
    button_id: str = Field("", max_length=200)
    media: MediaIn | None = None
    received_at: datetime | None = Field(None, description=RECEIVED_AT)
    simulated: bool = Field(False, description="Simulator traffic: tagged everywhere.")
    assign_to: Literal["", "order", "collector", "credit", "stock", "brand", "proof",
                       "mimi"] = Field(
        "", description="The owner names the helper (docs/HANDOFFS.md): overrides routing to "
                        "that helper's owner skills, still under every policy tier. Owner "
                        "messages only (422 otherwise). Same as '@Collector ...' at the start.")


class EventIn(BaseModel):
    """A system event from Go or the scheduler: a delivery done, a routine due."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1, max_length=200,
                          description="The sender's own event id. The same id twice is one turn.")
    kind: Literal["webhook", "routine"]
    name: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-z0-9_.]+$",
                      description="Webhook kind (delivery_completed, bank_credit, ...) or "
                                  "routine name (stock.weekly_check, ...).")
    shop_id: str = Field("", max_length=64, pattern=r"^[A-Za-z0-9_.-]*$",
                         description="The shop the event is about; empty for the distributor.")
    received_at: datetime | None = Field(None, description=RECEIVED_AT)
    simulated: bool = False
    amount_ngn: int | None = Field(None, ge=1, le=1_000_000_000,
                                   description="bank_credit only: the credited amount, whole "
                                               "naira (matches the screenshot payment)")


class VoiceReply(_Out):
    """The reply's own text, read aloud (RAI-290): fetch the audio from
    ``GET /v1/messages/{message_id}/voice`` and send it after the text."""

    script: str
    voice: str
    language: str
    # What the script says, read back from it by code: the same as the text.
    amounts_kobo: list[int]
    days: list[str]


class Reply(_Out):
    message_id: str
    channel: str
    shop_id: str
    text: str
    kind: str
    # ready: hand to the channel now. pending_approval: waits for the owner.
    status: str
    at: str
    voice: VoiceReply | None = None


class TurnOut(_Out):
    turn_id: str
    thread_id: str
    # queued | leased | done | failed (merged turns report their lead's state)
    state: str
    # done_quiet, done, asked_question, parked_for_approval, asked_to_clarify,
    # no_helper_yet:<helper>, stopped_by_owner, failed ...
    status: str
    duplicate: bool
    merged_into: str
    helper: str
    skill: str
    run_id: str = Field(..., description="The harness run id: the trace id in Langfuse.")
    attempts: int
    replies: list[Reply]
    route: dict[str, Any]
    error: str
    handoff: Handoff | None = Field(None, description="Who Mimi asked and how it went "
                                    "(docs/HANDOFFS.md). Present on every routed turn; "
                                    "status 'working' while it runs.")
    handback: Handback | None = Field(None, description="A helper returned the message to "
                                      "Mimi instead of answering; she re-routed it once.")
    card: Card | None = Field(None, description="The answer card (owner console): figures "
                              "read from the book by code.")
    chips: list[Chip] = Field(default_factory=list, description="2 or 3 safe next steps.")
    task_status: Literal["", "working", "needs_you", "done", "handed_back", "failed"] = ""


class Health(_Out):
    status: Literal["ok", "degraded"]
    db: str
    model: str
    queue_depth: int
    version: str


class Message(_Out):
    message_id: str
    direction: str
    sender: str
    channel: str
    shop_id: str
    text: str
    kind: str
    status: str
    turn_id: str
    simulated: bool
    at: str
    # A reply read aloud (RAI-290): fetch it from GET /v1/messages/{message_id}/voice.
    voice: bool = False
    # Demo mode: the bytes the owner attached in the console, from
    # GET /v1/messages/{message_id}/media.
    media: bool = False


class MediaOut(_Out):
    """A console upload kept for this demo copy: send ``ref``, ``sha256``,
    ``mime`` and ``kind`` as the turn's ``media``."""

    ref: str
    sha256: str
    mime: str
    kind: Literal["image", "voice", "document"]
    size: int


class Step(_Out):
    """One line of reasoning on the thread: a routing decision or a harness step.

    The model's private text is never included; ``short`` is written by code.
    """

    event_id: str
    turn_id: str
    run_id: str
    type: str
    step: int
    tool: str
    tier: str
    short: str
    model: str
    cost_kobo: int
    duration_ms: float
    at: str


class TurnSummary(_Out):
    turn_id: str
    state: str
    status: str
    helper: str
    skill: str
    run_id: str
    attempts: int
    simulated: bool
    created_at: str
    finished_at: str | None


class ThreadSummary(_Out):
    thread_id: str
    kind: str
    shop_id: str
    shop_name: str
    last_at: str
    last_text: str
    last_sender: str
    messages: int
    pending_approvals: int
    helper: str = Field(..., description="The helper on the thread's newest routed turn "
                        "(order, collector, credit, stock, brand, proof or mimi).")
    helper_name: str = Field(..., description="Its display name (Order, Collector, Credit, "
                             "Stock, Brand, Proof or Mimi).")
    status: Literal["needs_you", "running", "done"] = Field(
        ..., description="needs_you: a pending approval or a turn waiting on the owner; "
        "running: a turn queued or running; done: otherwise.")
    title: str = Field(..., description="A short title written by code (never shop text).")


class ThreadList(_Out):
    threads: list[ThreadSummary]
    total: int


class ThreadDetail(_Out):
    thread: ThreadSummary
    state: dict[str, Any]
    messages: list[Message]
    steps: list[Step]
    turns: list[TurnSummary]
    handoffs: list[Handoff] = Field(default_factory=list, description="Each turn's handoff, "
                                    "oldest first (docs/HANDOFFS.md).")
    inbox: list[InboxItem] = Field(default_factory=list, description="Handoffs a helper's own "
                                   "thread carries, newest first: received and sent "
                                   "(docs/HELPER-INBOX-CONTRACT.md).")


class TaskOut(_Out):
    """One helper task: a turn Mimi handed to a helper (docs/HANDOFFS.md)."""

    task_id: str
    turn_id: str
    thread_id: str
    helper: str
    helper_name: str
    skill: str
    status: Literal["working", "needs_you", "done", "handed_back", "failed"]
    reason: str
    handoff: Handoff | None
    handback: Handback | None = None
    card: Card | None = None
    chips: list[Chip] = Field(default_factory=list)
    created_at: str
    finished_at: str | None


class TaskList(_Out):
    tasks: list[TaskOut]
    total: int
    by_status: dict[str, int]


class InboxItem(_Out):
    """One piece of work a helper handed to another (docs/HELPER-INBOX-CONTRACT.md).

    ``from_helper`` is ``mimi`` for the owner's own ask routed by Mimi. Every
    field a person reads (``from_title``, ``to_title``, ``shop_name``, ``ask``,
    ``reason``, ``outcome``, ``result``) is a sentence written by code; the
    rest are machine values."""

    task_id: str
    contract: str
    from_helper: str
    from_title: str
    to_helper: str
    to_title: str
    skill: str
    shop_id: str
    shop_name: str
    ask: str
    state: Literal["queued", "working", "done", "handed_back", "failed", "refused"]
    reason: str
    outcome: str
    result: str
    chain: list[str] = Field(default_factory=list)
    parent_task_id: str = ""
    thread_id: str = ""
    turn_id: str = ""
    created_at: str
    finished_at: str = ""


class HelperLine(_Out):
    """The one honest sentence per helper the sidebar needs, from real data.

    The console derives the face state from ``open`` and ``waiting_on_owner``;
    the API never stores a state word that can drift."""

    helper: str
    helper_name: str
    open: int
    waiting_on_owner: int
    outcome: str


class InboxList(_Out):
    items: list[InboxItem]
    total: int
    by_state: dict[str, int]
    helpers: list[HelperLine] = Field(default_factory=list)


class ActivityStep(_Out):
    """One step of the helper's work, in plain words for the Activity row.

    ``short`` is the one line the console shows (tool names in plain words,
    names for ids, naira for money); ``name`` is the tool or event behind it.
    """

    name: str
    kind: str
    status: str
    short: str
    ms: float = 0.0
    at: str = ""


class ActivityRow(_Out):
    """One row of the console's Activity view: a turn Mimi handed to a helper."""

    task_id: str
    turn_id: str
    thread_id: str
    helper: str
    helper_name: str
    skill: str
    status: Literal["working", "needs_you", "done", "handed_back", "failed"]
    tab: Literal["running", "waiting", "done"] = Field(
        ..., description="The Activity tab: running (working), waiting (needs_you), "
        "done (done, handed_back, failed).")
    what: str = Field(..., description="What the helper is doing, in plain words "
                      "written by code, never shop text.")
    shop_name: str = ""
    # When the turn started, Africa/Lagos with its zone.
    started_at: str
    finished_at: str | None
    steps: list[ActivityStep] = Field(default_factory=list)
    pending_approvals: int = 0


class ActivityList(_Out):
    rows: list[ActivityRow]
    total: int
    by_tab: dict[str, int]


class StopOut(_Out):
    """The answer to stopping one turn: the turn's state after the stop."""

    turn_id: str
    # queued | leased | done | failed (merged turns report their lead's state)
    state: str
    # The turn's status (stopping while the worker parks it, stopped_by_owner
    # once it has, its old status when it already finished).
    status: str
    # True when the stop flagged a turn that had not finished yet.
    stopped: bool
    message: str = Field(..., description="One plain line for the console, written by code.")


class ShopSummary(_Out):
    shop_id: str
    name: str
    owner: str
    area: str
    shop_type: str
    size: str
    rep_id: str
    phone_last4: str
    credit_limit_kobo: int
    credit_limit_naira: str
    outstanding_kobo: int
    outstanding_naira: str
    overdue_kobo: int
    overdue_naira: str
    open_invoices: int
    simulated: bool
    # Whole naira and days (demo wiring, 2 Nov 2026): days since the oldest
    # overdue invoice fell due, 0 when nothing is overdue.
    credit_limit_ngn: int = 0
    outstanding_ngn: int = 0
    overdue_ngn: int = 0
    overdue_days: int = 0


class ShopList(_Out):
    shops: list[ShopSummary]
    total: int
    # Summed once in code (mimi.owed): the same figures Collector and the cards use.
    owed_total_kobo: int = 0
    owed_total_naira: str = ""
    overdue_total_kobo: int = 0
    overdue_total_naira: str = ""


class DeliveryLineOut(_Out):
    sku: str
    name: str
    unit: str
    quantity: int
    unit_price_kobo: int
    line_total_kobo: int
    # The batch's expiry day (Africa/Lagos) and batch code from the delivery
    # note, when it gave them (sales tracking).
    expires_on: str | None = None
    batch: str = ""


class DeliveryOut(_Out):
    delivery_id: str
    delivered_on: str
    rep_id: str
    total_kobo: int
    total_naira: str
    status: str
    # RAI-295: verified | likely | estimated, and the word shown beside it.
    proof_level: Literal["verified", "likely", "estimated"]
    proof: str
    proof_policy_version: str
    lines: list[DeliveryLineOut]


class ShopDetail(_Out):
    shop: ShopSummary
    thread_id: str
    receivables: list[dict[str, Any]]
    payments: list[dict[str, Any]]
    promises: list[dict[str, Any]]
    deliveries: list[DeliveryOut]
    disputes: list[dict[str, Any]]


class Draft(_Out):
    text: str
    channel: str
    status: str


class ApprovalOut(_Out):
    approval_id: str
    status: str
    helper: str
    skill: str
    shop_id: str
    thread_id: str
    turn_id: str
    tool: str
    intent_kind: str
    preview: str = Field(description="As the owner reads it: shop and rep names for ids, "
                                     "money as ₦1,234,500 (mimi.api.display)")
    preview_raw: str = Field("", description="The preview as the helper's dry run wrote it")
    shop_name: str = ""
    flags: list[Literal["over_credit_limit"]] = Field(
        default_factory=list, description="over_credit_limit: approving this order would put "
                                          "the shop past its credit limit (checked in code)")
    over_limit_by_kobo: int = 0
    payload: dict[str, Any]
    draft: Draft | None
    created_at: str
    decided_at: str | None
    decided_by: str
    note: str
    # A shop's correction made a new version (RAI-301): the ids it replaced
    # and that replaced it. A superseded or withdrawn item cannot be decided.
    supersedes: str | None = None
    superseded_by: str | None = None
    # approve: Mimi carries it out once approved. hand_off: the owner's rule
    # says the owner does this step by hand; approving records it was done.
    handling: str = "approve"
    # Demo wiring (2 Nov 2026): zoho_payment, credit_over_limit, or the tool name.
    kind: str = ""
    blocked: bool = Field(False, description="credit_over_limit while pending: the order is "
                                             "held over the shop's limit")
    result: dict[str, Any] | None = Field(None, description="What code did once approved "
                                                            "(zoho_payment_id, change_id, "
                                                            "undo_available)")


class ApprovalList(_Out):
    approvals: list[ApprovalOut]
    total: int


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field("", max_length=2000)
    by: str = Field("owner", max_length=100, description="Who decided, for the audit line.")
    option: Literal["move", "promo", "pickup"] | None = Field(
        None, description="slow_stock approvals only: the option to carry out (default the "
                          "draft's recommended one). 409 when it is not one of its options.")


class SellThroughRow(_Out):
    """One shop and product (sales tracking, 3 Nov 2026). Figures by code;
    display strings carry names and ₦ only, never internal ids."""

    shop_id: str
    shop_name: str
    area: str
    sku: str
    item: str
    unit: str
    period_days: int
    sold_units: int = Field(description="Sold over sold_over_days: from two shop counts "
                                        "(count + delivered - later count) when there are two, "
                                        "else the estimate")
    sold_over_days: int
    sold_method: Literal["counts", "estimate"]
    sold_value_ngn: int
    proof_level: Literal["verified", "likely", "estimated"]
    proof_grade: Literal["verified", "estimated", "unverified"]
    proof: str = Field(description="The word shown beside the figure, e.g. 'estimated (shop count)'")
    units_left: int = Field(description="Estimated units on the shelf (never a shop's count "
                                        "shown as fact)")
    days_of_cover: int | None
    days_to_expiry: int | None
    expires_on: str | None
    status: Literal["low", "ok", "slow", "near_expiry"]
    status_word: str
    watch: Literal["", "near_expiry", "overstock"] = Field(
        "", description="A low-confidence slow reading: a hint, no alert")
    confidence: Literal["low", "medium", "high"]
    at_risk_units: int
    at_risk_ngn: int
    last_count_on: str | None
    note: str


class SellThroughOut(_Out):
    as_of: str
    period_days: int
    rows: list[SellThroughRow]
    total: int
    counts: dict[str, int] = Field(description="Rows per status, before any status filter")
    at_risk_ngn: int
    at_risk_text: str


class SlowCheckOut(_Out):
    week: str
    opened: list[str] = Field(description="slow_stock approval ids opened by this run "
                                          "(empty when this week's were already opened)")
    skipped: list[dict[str, str]]
    checked: int


class DecisionOut(_Out):
    approval: ApprovalOut
    follow_up: TurnOut | None


class CreditLimit(_Out):
    shop_id: str
    name: str
    credit_limit_kobo: int
    credit_limit_naira: str
    outstanding_kobo: int
    outstanding_naira: str
    available_kobo: int
    available_naira: str
    used_pct: int
    over_limit: bool = False
    over_limit_by_kobo: int = 0
    over_limit_by_naira: str = ""
    credit_limit_ngn: int = 0
    outstanding_ngn: int = 0
    overdue_ngn: int = 0
    overdue_days: int = 0


class CreditPayment(_Out):
    """A recent payment with its proof (stored level and the demo's grade) and
    its shop's overdue figures at read time."""

    payment_id: str
    shop_id: str
    shop_name: str
    amount_ngn: int
    paid_on: str
    method: str
    proof_level: Literal["verified", "likely", "estimated"]
    proof_grade: Literal["verified", "estimated", "unverified"]
    proof: str
    overdue_days: int
    overdue_ngn: int


class CreditRequest(_Out):
    request_id: str
    shop_id: str
    amount_kobo: int
    amount_naira: str
    reason: str
    status: str
    approval_id: str | None
    created_at: str


class CreditOut(_Out):
    limits: list[CreditLimit]
    requests: list[CreditRequest]
    payments: list[CreditPayment] = Field(default_factory=list)


class CreditLimitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_ngn: int = Field(..., ge=0, le=1_000_000_000, description="The new limit, whole naira.")
    reason: str = Field("", max_length=500)


class CreditLimitOut(_Out):
    shop_id: str
    old_limit_ngn: int
    new_limit_ngn: int
    change_id: str
    undone: bool = False


class ErrorOut(_Out):
    detail: str


class DemoLimit(_Out):
    """Why a demo request was refused, in one fixed shape the console shows."""

    code: Literal["visitor_daily_cap", "global_daily_cap", "paused", "too_many_sessions", ""]
    scope: Literal["visitor", "global", "demo"]
    title: str
    message: str
    spent_kobo: int | None
    cap_kobo: int | None
    spent_naira: str | None
    cap_naira: str | None
    resets_at: str
    retry: Literal["tomorrow", "later"]


class DemoLimitError(_Out):
    detail: str
    demo_limit: DemoLimit
class ConnectorConnection(_Out):
    connection_id: str
    status: str
    region: str
    connected_at: str
    revoked_at: str | None


class ConnectorToolkit(_Out):
    toolkit: str
    label: str
    blurb: str
    reads: list[str]
    writes: list[str]
    connections: list[ConnectorConnection]


class ConnectorList(_Out):
    connectors: list[ConnectorToolkit]


class ConnectorConnectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region: str = Field("", max_length=8,
                        description="The Zoho account region (com, eu, in, cn or au).")


class ConnectorConnectOut(_Out):
    redirect_url: str
    state: str


class ConnectorSyncPreview(_Out):
    rows_read: int
    rows_mapped: int
    errors: list[str]
    finished_at: str | None


class ConnectorPreviewRow(_Out):
    shop_id: str
    sku: str
    item: str
    quantity: int
    counted_on: str


class ConnectorDetail(_Out):
    connection_id: str
    toolkit: str
    status: str
    region: str
    connected_at: str
    revoked_at: str | None
    last_sync: ConnectorSyncPreview | None
    preview: list[ConnectorPreviewRow]


class Liveness(_Out):
    status: Literal["ok"]


class DemoStatus(_Out):
    """A demo caller's session and today's spend against the caps (kobo)."""

    expires_at: str | None
    ip_source: str = ""
    resets_at: str = ""
    ip_spent_kobo: int
    ip_cap_kobo: int
    global_spent_kobo: int
    global_cap_kobo: int
    paused: bool


class MeOut(_Out):
    tenant_id: str
    role: str
    mode: Literal["live", "demo"]
    demo: DemoStatus | None


class DemoCaps(_Out):
    ip_daily_cap_kobo: int
    global_daily_cap_kobo: int


class DemoSessionOut(_Out):
    """A demo session: send ``token`` as ``Authorization: Bearer`` until ``expires_at``."""

    token: str
    tenant_id: str
    mode: Literal["demo"]
    expires_at: str
    ttl_s: float
    caps: DemoCaps


# ------------------------------------------------------------------ mentions


class Mention(_Out):
    """One row of the console's @ picker: a helper, a shop or a rep."""

    kind: Literal["helper", "shop", "rep"]
    id: str = Field(description="helper name, shop_id or rep_id")
    name: str
    subtitle: str = Field("", description="shop: area and rep; rep: areas; helper: what it does")
    mention: str = Field(description="What the console inserts into the message: '@Collector', "
                                     "or the shop's or rep's name as written in the book")
    assign_to: str = Field("", description="helper rows only: the value for assign_to on "
                                           "POST /v1/turns")


class MentionList(_Out):
    mentions: list[Mention]
    total: int


# --------------------------------------------------------------- the rulebook


class PolicyRuleOut(_Out):
    """One step in the owner's rulebook (RAI-323)."""

    action_type: str
    helper: str
    label: str
    # act | act_if_preapproved | ask | hand_off; never for a built-in stop.
    behaviour: str
    default: str
    # A built-in stop: no rule, API call or message can change it.
    locked: bool
    allowed: list[str]
    changed_by: str
    changed_at: str


class PolicyChangeOut(_Out):
    action_type: str
    from_: str = Field(alias="from")
    to: str
    by: str
    at: str


class PolicyOut(_Out):
    rules: list[PolicyRuleOut]
    # False in demo mode or with MIMI_POLICY_RULES=0: the list is read-only.
    editable: bool
    changes: list[PolicyChangeOut]


class PolicyRuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: str = Field(max_length=64)
    behaviour: Literal["act", "act_if_preapproved", "ask", "hand_off"]


class PolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[PolicyRuleIn] = Field(min_length=1, max_length=50)
    # Who changed it, for the log (the console sends the owner's name).
    by: str = Field(default="owner", max_length=100)
