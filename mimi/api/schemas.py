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
        ..., description="needs_you: a pending approval, or a turn waiting on a person "
        "(a question to answer, a park, a denial); running: a turn queued or running; "
        "done: otherwise.")
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
    # A payment (or a return credit) bigger than the shop owed, held on its
    # account until it is allocated to an invoice.
    on_account_kobo: int = 0
    on_account_naira: str = ""
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


# ------------------------------------------- returns (feat/returns-and-exposure)


class ReturnLineOut(_Out):
    """One line of a return. The console maps: product=item, quantity=
    quantity_drafted, unit=unit, collected=quantity_collected, came back=
    quantity_returned, disposition=disposition."""

    line_no: int
    sku: str
    item: str
    unit: str
    reason: str
    quantity_drafted: int
    quantity_collected: int | None = None
    quantity_returned: int | None = None
    unit_price_kobo: int
    value_drafted_kobo: int
    value_returned_kobo: int | None = None
    condition: Literal["", "resellable", "damaged", "expired"] = ""
    disposition: Literal["", "resell", "write_off", "return_to_supplier"] = ""
    expires_on: str | None = Field(None, description="From the distributor's own invoice "
                                                     "line when the note carried one")
    batch: str = ""
    expiry_source: Literal["invoice", "estimated"] = Field(
        "estimated", description="invoice: the delivery note carried it. estimated: it "
                                 "did not, so nothing is claimed off a shop count")


class ReturnTransition(_Out):
    status: str
    at: str
    by: str
    note: str


class ReturnOut(_Out):
    return_id: str
    shop_id: str
    shop_name: str
    source: Literal["slow_stock", "manual"]
    approval_id: str | None = Field(None, description="The approved pickup it opened from")
    reason: str
    status: Literal["drafted", "approved", "collected", "inspected", "settled", "cancelled"]
    lines: list[ReturnLineOut]
    value_drafted_kobo: int
    value_collected_kobo: int
    value_returned_kobo: int = Field(description="Value of what came back at inspection")
    recovered_kobo: int = Field(description="Value off the lines that sell again or go "
                                            "back to the supplier; a write-off recovers 0")
    pickup_cost_kobo: int | None = Field(None, description="The owner's configured pickup "
                                                           "cost, or null when not set")
    saving_kobo: int | None = Field(None, description="recovered less the pickup cost, or "
                                                      "null when no pickup cost is set")
    saving_basis: str = ""
    credit_kobo: int | None = None
    credit_approval_id: str | None = None
    credit_applied_kobo: int | None = Field(None, description="Off the shop's open invoices")
    credit_on_account_kobo: int | None = Field(None, description="Held on the shop's account "
                                                                 "because it owed nothing")
    transitions: list[ReturnTransition]
    created_at: str
    updated_at: str


class ReturnList(_Out):
    returns: list[ReturnOut]
    total: int


class ReturnOpenLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(..., max_length=64)
    quantity: int = Field(..., ge=1, le=1_000_000)
    reason: str = Field("", max_length=300)


class ReturnOpenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field("", max_length=64, description="An approved slow-stock pickup "
                                                            "option; opens the return from it")
    shop_id: str = Field("", max_length=64)
    reason: str = Field("", max_length=500)
    lines: list[ReturnOpenLine] = Field(default_factory=list)


class CollectLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_no: int = Field(..., ge=1)
    quantity: int = Field(..., ge=0)


class CollectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by: str = Field("rep", max_length=100)
    lines: list[CollectLine] = Field(default_factory=list, description="Omit to take what "
                                                                       "was drafted")


class InspectLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_no: int = Field(..., ge=1)
    quantity_returned: int = Field(..., ge=0, description="What came back; never above what "
                                                          "was collected")
    condition: Literal["", "resellable", "damaged", "expired"] = ""
    disposition: Literal["", "resell", "write_off", "return_to_supplier"] = ""


class InspectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by: str = Field("owner", max_length=100)
    lines: list[InspectLine] = Field(default_factory=list, description="Every line must be "
                                                                       "named")


class ReturnDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by: str = Field("owner", max_length=100)
    note: str = Field("", max_length=2000)


class ReturnSettleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by: str = Field("owner", max_length=100)
    amount_kobo: int | None = Field(None, ge=1, description="The credit; default is what "
                                                            "came back, never more")


class ReturnSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pickup_cost_kobo: int = Field(..., ge=0, le=100_000_000_000,
                                  description="What a collection trip costs, in kobo")


class ReturnSettingsOut(_Out):
    pickup_cost_kobo: int | None = None
    pickup_cost_naira: str = ""
    note: str = ""


class ExposureBucket(_Out):
    key: str
    label: str
    from_days: int | None
    to_days: int | None
    weight_pct: int


class ExposureRow(_Out):
    shop_id: str
    shop_name: str
    outstanding_kobo: int = Field(description="What the shop owes; the money figure")
    outstanding_naira: str
    overdue_kobo: int
    overdue_naira: str
    oldest_days_overdue: int
    buckets: dict[str, int] = Field(description="Outstanding per ageing band, in kobo")
    exposure_kobo: int = Field(description="Risk-weighted: outstanding scaled by each band's "
                                           "weight. An assumption, not money owed")
    exposure_naira: str
    verified_paid_kobo: int = Field(description="Bank-verified payments only")
    shown_not_counted_kobo: int = Field(description="Reported payments, not in the bank feed")
    open_invoices: int
    confidence: Literal["low", "medium", "high"] = Field(
        description="From the count of bank-verified payments in the last 8 weeks: 8 or more "
                    "high, 4 to 7 medium, fewer than 4 low")
    note: str


class ExposureOut(_Out):
    as_of: str
    policy_version: str
    buckets: list[ExposureBucket]
    note: str = Field(description="The weights, stated plainly")
    shops: list[ExposureRow]
    total: ExposureRow = Field(description="Every shop summed; confidence is the weakest "
                                           "shop's with exposure")


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


class BooksShopAgreement(_Out):
    """One shop's two-books comparison. ``difference_*`` is Mimi minus
    Zoho: positive means Mimi's book is higher. ``match`` says how the row
    was matched: ``id`` (the ``zc-`` convention), ``name``, ``ambiguous``
    (two Zoho customers carried the shop's name, so neither was picked),
    ``none`` (Zoho does not know the shop) or ``zoho_only`` (Zoho's
    customer, with no Mimi shop behind it)."""

    shop_id: str
    shop_name: str
    match: Literal["id", "name", "ambiguous", "none", "zoho_only"] = "none"
    mimi_outstanding_kobo: int
    mimi_outstanding_naira: str
    zoho_outstanding_kobo: int | None
    zoho_outstanding_naira: str
    difference_kobo: int | None
    difference_naira: str


class BooksAgreementTotal(_Out):
    mimi_outstanding_kobo: int
    mimi_outstanding_naira: str
    zoho_outstanding_kobo: int | None
    zoho_outstanding_naira: str
    difference_kobo: int | None
    difference_naira: str


class BooksAgreementOut(_Out):
    """The two-books trust read. When ``zoho_connected`` is false the
    console shows no Zoho figure at all: every zoho and difference field
    is null and ``note`` says why in plain words."""

    as_of: str
    zoho_connected: bool
    currency: str
    shops: list[BooksShopAgreement]
    total: BooksAgreementTotal
    note: str


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


class RoutineOut(_Out):
    """One scheduled routine on the console's rail (4 Nov 2026).

    ``name``, ``schedule`` and ``last_finding`` are sentences written by code
    and rendered as written. ``last_run_at`` and ``next_run_at`` are ISO 8601
    with an explicit zone (Africa/Lagos); ``last_run_at`` is empty when the
    routine has never run, and ``last_finding`` is empty then too (the rail
    says "Not run yet" from the timestamps, not twice).
    """

    id: str
    name: str
    schedule: str
    paused: bool
    # Present only when paused (the route excludes the None).
    paused_reason: str | None = None
    last_run_at: str
    next_run_at: str
    last_finding: str
    # Present only when the last finding is about exactly one shop, so the
    # console can link it. Never guessed: several shops or the whole book
    # leave it out and the sentence renders as plain text.
    last_finding_shop_id: str | None = None


class RoutineList(_Out):
    routines: list[RoutineOut]
    as_of: str


class RoutinePauseIn(BaseModel):
    """Why a routine is being paused (the skip records it); optional."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field("", max_length=300)
    by: str = Field("", max_length=100)


class RoutineToggleOut(_Out):
    id: str
    name: str
    paused: bool


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


# ----------------------------------------------------------- computer tasks
# A computer task is a goal, an approved domain and a plan code built from the
# task's own fields. The owner approves the plan once; code runs it under the
# guardrails. Nothing here is a model choice and nothing here moves money.

_COMPUTER_KINDS = "price_check | order_list | bank_statement | gov_form | generic"


class ComputerTaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(..., min_length=1, max_length=600,
                      description="What the distributor wants done, in their own words.")
    url: str = Field(..., min_length=1, max_length=2000,
                     description="The page to start on. It must be on the approved domain.")
    domain: str = Field(..., min_length=1, max_length=255,
                        description="The only domain the task may use, with its subdomains.")
    kind: Literal["price_check", "order_list", "bank_statement", "gov_form", "generic"] = Field(
        "generic", description=f"The task kind: {_COMPUTER_KINDS}.")
    inputs: dict[str, str] = Field(
        default_factory=dict, max_length=20,
        description="Values the owner typed for a form task (a gov_form fills only these, "
                    "never a password or a code).")


class ComputerPlanStep(_Out):
    action: str = Field(..., description="The step in plain words. This is what "
                                         "the console renders: never a code.")
    code: str = Field("", description="Our own name for the step (a machine verb, "
                                      "for example 'goto'). The console does not "
                                      "render this field.")
    note: str = ""
    url: str = ""
    target: str = ""
    field: str = ""
    value_key: str = ""
    direction: str = ""
    amount: int = 0


class ComputerTask(_Out):
    task_id: str
    status: Literal["proposed", "running", "awaiting_owner", "done", "stopped",
                    "stopped_runaway", "failed"] = Field(
        ..., description="One of exactly these seven. A task is never 'queued': "
                         "that is an internal queue word, not a task status.")
    kind: str
    goal: str
    url: str
    domain: str
    stop_reason: str = Field(
        "", description="A machine code for the log: off_domain, forbidden_page, "
                        "budget_steps, budget_seconds, login_wall, "
                        "stopped_by_owner, unsupported_step, engine_error, empty "
                        "while the task has not stopped. Never render this.")
    note: str = Field(
        "", description="The sentence for a person, and the one to render. It is "
                        "always plain words when the task has stopped, whatever "
                        "the stop_reason says.")
    created_at: str
    started_at: str = ""
    finished_at: str = ""
    plan: list[ComputerPlanStep] = Field(default_factory=list)
    inputs: dict[str, str] = Field(default_factory=dict)


class ComputerStep(_Out):
    index: int
    action: str = Field(..., description="What the step did, in plain words. This "
                                         "is what the console renders: never a code.")
    code: str = Field("", description="Our own name for the step (a machine verb, "
                                      "for example 'goto'). The console does not "
                                      "render this field.")
    url: str = ""
    note: str = ""
    screenshot: str = Field("", description="An artifact name. Its bytes are at "
                                            "GET /v1/computer/tasks/{task_id}/artifacts/{name}")


class ComputerArtifact(_Out):
    name: str
    kind: Literal["screenshot", "text", "file"]
    mime: str = ""
    size: int = 0
    sha256: str = ""


class ComputerTaskDetail(ComputerTask):
    steps: list[ComputerStep] = Field(default_factory=list)
    artifacts: list[ComputerArtifact] = Field(default_factory=list)
    live_view_url: str | None = Field(
        None, description="A signed link to watch this task's session while one "
                          "is open: the real screen, polled by the console in a "
                          "new tab. It is per session, it dies with the session, "
                          "and it holds no key of ours. Null once the task ends.")


class ComputerKind(_Out):
    kind: str = Field(..., description="The kind string to send in a task's kind field.")
    label: str = Field("", description="What that kind of task does, in plain words.")


class ComputerKindList(_Out):
    kinds: list[ComputerKind] = Field(default_factory=list)


class ComputerTaskSummary(_Out):
    task_id: str
    goal: str
    domain: str
    status: str
    created_at: str
    step_count: int = 0
    stop_reason: str = Field(
        "", description="A machine code, as on the task detail. Never render "
                        "this: the sentence beside it is the one for a person.")
    note: str = Field(
        "", description="The sentence for a person, and the one to render in a "
                        "list: it is always plain words when the task has "
                        "stopped or paused.")


class ComputerTaskList(_Out):
    tasks: list[ComputerTaskSummary] = Field(default_factory=list)


class ComputerProposal(_Out):
    """The proposal the owner approves. ``plan`` is what will run, step by step."""

    task_id: str
    status: Literal["proposed"]
    plan: list[ComputerPlanStep] = Field(default_factory=list)
