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


class ShopList(_Out):
    shops: list[ShopSummary]
    total: int


class DeliveryLineOut(_Out):
    sku: str
    name: str
    unit: str
    quantity: int
    unit_price_kobo: int
    line_total_kobo: int


class DeliveryOut(_Out):
    delivery_id: str
    delivered_on: str
    rep_id: str
    total_kobo: int
    total_naira: str
    status: str
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
    preview: str
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


class ApprovalList(_Out):
    approvals: list[ApprovalOut]
    total: int


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field("", max_length=2000)
    by: str = Field("owner", max_length=100, description="Who decided, for the audit line.")


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


class ErrorOut(_Out):
    detail: str


class Liveness(_Out):
    status: Literal["ok"]


class DemoStatus(_Out):
    """A demo caller's session and today's spend against the caps (kobo)."""

    expires_at: str | None
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
