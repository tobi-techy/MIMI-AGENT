"""The harness's typed records: events, intents, drafts and the run result.

These are Pydantic models because they cross a process boundary: the system
of record (Go, here its stand-ins) reads them as JSON, the console streams the
events, and the job store saves the loop state between steps. Field names are
the contract; keep them in step with the OpenAPI document when the Go side
lands.

Money is integer kobo everywhere. A float never holds an amount.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Risk tiers (architecture doc, "Trust gates"). Fixed in Go policy, never the
# model's judgement: a tier the model names is ignored.
Tier = Literal["auto", "notify", "approve", "never"]
ToolKind = Literal["read", "write_intent"]
# Cheapest path first (RAI-279 always-on rule 5), in this order.
CostTier = Literal["knowledge", "go_api", "web", "computer", "ask_owner"]
COST_ORDER: tuple[CostTier, ...] = ("knowledge", "go_api", "web", "computer", "ask_owner")

# Where a turn came from (the router, RAI-280, will set it; the harness only
# carries it into the context, the events and the trace).
WakeSource = Literal[
    "owner_message",
    "shop_message",
    "rep_message",
    "routine",
    "webhook",
    "helper_finished",
    "scheduled_check_in",
    "approval_answered",
]

RunStatus = Literal[
    "done",
    "done_quiet",
    "parked_for_approval",
    "parked_for_timer",
    "asked_question",
    "stopped_budget",
    "stopped_killed",
    "failed",
]

EventType = Literal[
    "run_started",
    "step_started",
    "model_called",
    "model_fallback",
    "model_output_invalid",
    "tool_called",
    "tool_result",
    "tool_refused",
    "repair_requested",
    "field_ignored",
    "intent_proposed",
    "draft_proposed",
    "budget_hit",
    "cap_hit",
    "kill_switch",
    "step_finished",
    "run_finished",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Event(_Frozen):
    """One line in the thread's append-only event log.

    ``event_id`` is derived from the run, the step and the position, so a
    re-run of a crashed step writes the same ids and the log dedupes.
    """

    event_id: str
    run_id: str
    thread_id: str
    job_id: str
    seq: int
    step: int
    type: EventType
    at: datetime
    tool: str = ""
    tier: str = ""
    scope: str = ""
    # One short line for the console's step lines. Never a figure the model
    # wrote: figures come from tool outputs built in code.
    short: str = ""
    duration_ms: float = 0.0
    cost_kobo: int = 0
    model: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class IntentRecord(_Frozen):
    """A typed proposal from a write tool. Go validates, dedupes and gates it.

    ``idempotency_key`` is the business key: the tool's declared fields plus
    the tenant (the same reminder for the same shop, invoice and day has one
    key, whichever job proposes it). ``attempt_key`` adds the job and step,
    so a resumed or taken-over job replays exactly what it proposed.
    """

    tool: str
    intent_kind: str
    tier: Tier
    scope: str
    tenant_id: str
    shop_id: str = ""
    idempotency_key: str
    attempt_key: str
    payload: dict[str, Any]
    # What dry_run says would happen (the verifier and shadow mode read it).
    preview: str = ""
    step: int = 0
    # Shadow mode (RAI-235): Go records the intent and applies nothing.
    shadow: bool = False


Channel = Literal["shop_whatsapp", "owner_whatsapp", "owner_console", "rep_whatsapp"]
DraftKind = Literal["reply", "update", "question", "receipt", "reminder", "report"]


class OutboundDraft(_Frozen):
    """Text for a person. Only ``send_to_user`` makes one; model text never."""

    channel: Channel
    body: str
    kind: DraftKind
    tier: Tier
    tenant_id: str
    shop_id: str = ""
    idempotency_key: str
    step: int = 0


class RunEvent(_Frozen):
    """What woke the helper (the router's envelope, trimmed to what the loop needs)."""

    event_id: str
    tenant_id: str
    thread_id: str
    wake_source: WakeSource
    text: str = ""
    # Who sent ``text``. Shop and rep text is untrusted data (RAI-239).
    sender: Literal["owner", "shop", "rep", "system"] = "owner"
    shop_id: str = ""
    at: datetime
    job_id: str = ""


class Spend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: int = 0
    model_calls: int = 0
    seconds: float = 0.0
    kobo: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class RunResult(_Frozen):
    run_id: str
    job_id: str
    thread_id: str
    helper: str
    wake_source: WakeSource
    status: RunStatus
    # One line for the trace: why the turn ended (and, for done_quiet, why
    # nothing was said).
    reason: str
    intents: tuple[IntentRecord, ...] = ()
    outbound_drafts: tuple[OutboundDraft, ...] = ()
    events: tuple[Event, ...] = ()
    # The owner question when the run stopped on a budget, cap or approval;
    # figures from code.
    owner_question: str = ""
    follow_up_at: datetime | None = None
    spend: Spend = Field(default_factory=Spend)
    toolset_hash: str = ""
    registry_version: str = ""
    model_calls: int = 0
    loop_overhead_ms: float = 0.0


class StepResult(_Frozen):
    """One step in the durable job runner's mode (``Harness.step``)."""

    finished: bool
    result: RunResult | None
    new_intents: tuple[IntentRecord, ...] = ()
    new_drafts: tuple[OutboundDraft, ...] = ()
    new_events: tuple[Event, ...] = ()
    plan: tuple[str, ...] = ()
    todo: tuple[str, ...] = ()
    working_note: str = ""
    cost_kobo: int = 0
    state: dict[str, Any] = Field(default_factory=dict)


class Denial(_Frozen):
    """A typed refusal, returned instead of data. Always says what to do next."""

    denied: Literal[True] = True
    reason: str
    next_step: str
