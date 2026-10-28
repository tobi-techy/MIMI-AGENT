"""Request and response bodies of the HTTP API (the OpenAPI contract).

Money is integer kobo (``*_kobo``) with a display string beside it
(``*_naira``) computed in code. Times are ISO 8601 with their zone.
Phone numbers never leave the API in full: only the last four digits.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class ThreadList(_Out):
    threads: list[ThreadSummary]
    total: int


class ThreadDetail(_Out):
    thread: ThreadSummary
    state: dict[str, Any]
    messages: list[Message]
    steps: list[Step]
    turns: list[TurnSummary]


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
