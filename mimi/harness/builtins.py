"""Harness built-in tools every helper gets: speaking, asking, saving the plan.

* ``send_to_user``: the only way text reaches a person (RAI-278 always-on
  rule 3). Plain model text is private. ``ends_turn=true`` on the last send
  stops the loop with no further model call.
* ``ask_question``: one clear question to the owner or the shop; ends the
  turn ``asked_question``.
* ``update_plan``: the one dedicated tool for saving state (plan, to-do,
  working note), so reviewers and the console can see exactly what changed.

All three are write tools: they return intents with idempotency keys and a
dry run, and Go sends the message after the step is saved.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mimi.harness.registry import ToolContext, ToolRegistry
from mimi.harness.style import clean_reply
from mimi.tz import lagos_day as _lagos_day


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


def lagos_day(ctx: ToolContext) -> str:
    """Today in Africa/Lagos, as an ISO date (the day every key and rule uses)."""

    return _lagos_day(ctx.now).isoformat()


def digest(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).lower().encode()).hexdigest()[:16]


class SendToUserIn(_In):
    channel: Literal["shop_whatsapp", "owner_whatsapp", "owner_console", "rep_whatsapp"]
    body: str = Field(min_length=1, max_length=1200)
    kind: Literal["reply", "update", "receipt", "reminder", "report"] = "reply"
    shop_id: str = Field("", description="Required when channel is shop_whatsapp.")
    ends_turn: bool = Field(False, description="True on your last message of the turn.")


class SendToUserOut(_Out):
    channel: str
    body: str
    kind: str
    shop_id: str
    ends_turn: bool
    date: str
    body_digest: str


def send_to_user(ctx: ToolContext, args: SendToUserIn) -> SendToUserOut:
    """Send a message to a person: the owner (console or WhatsApp), a shop or a rep. This is the only way anyone sees what you write; your plain text is private. Every naira figure in body must be copied from a tool result. Set ends_turn=true on the last message of the turn."""
    from mimi.harness.errors import ToolError

    if args.channel == "shop_whatsapp" and not args.shop_id:
        raise ToolError("A shop message needs shop_id.",
                        "Call send_to_user again with the shop_id.")
    # The style guard (RAI-303): no dashes, no filler; figures never change.
    body = clean_reply(args.body.strip(), where="send_to_user")
    return SendToUserOut(
        channel=args.channel, body=body, kind=args.kind, shop_id=args.shop_id,
        ends_turn=args.ends_turn, date=lagos_day(ctx),
        body_digest=digest(body),
    )


def _send_scope(args: SendToUserIn) -> str:
    return {"shop_whatsapp": "shop:messages", "rep_whatsapp": "org:reps"}.get(
        args.channel, "org:owner")


def _send_dry(ctx: ToolContext, args: SendToUserIn, out: SendToUserOut) -> str:
    who = f"{out.channel}:{out.shop_id}" if out.shop_id else out.channel
    return f"Would send to {who}: {out.body[:140]}"


class AskQuestionIn(_In):
    to: Literal["owner", "shop"] = "owner"
    question: str = Field(min_length=3, max_length=400)
    shop_id: str = ""


class AskQuestionOut(_Out):
    to: str
    question: str
    shop_id: str
    date: str
    question_digest: str


def ask_question(ctx: ToolContext, args: AskQuestionIn) -> AskQuestionOut:
    """Ask the owner (or the shop) one clear question when you are unsure, instead of guessing. Ends the turn until they answer."""
    question = clean_reply(args.question.strip(), where="ask_question")
    return AskQuestionOut(to=args.to, question=question, shop_id=args.shop_id,
                          date=lagos_day(ctx), question_digest=digest(question))


def _ask_scope(args: AskQuestionIn) -> str:
    return "shop:messages" if args.to == "shop" else "org:owner"


def _ask_dry(ctx: ToolContext, args: AskQuestionIn, out: AskQuestionOut) -> str:
    return f"Would ask the {out.to}: {out.question[:140]}"


class UpdatePlanIn(_In):
    plan: list[str] = Field(default_factory=list, max_length=12)
    todo: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field("", max_length=800)


class UpdatePlanOut(_Out):
    plan: list[str]
    todo: list[str]
    note: str
    job_id: str
    step: int


def update_plan(ctx: ToolContext, args: UpdatePlanIn) -> UpdatePlanOut:
    """Save this job's plan, to-do list and a short working note so the next step (or the owner) can see them. The only tool that saves state."""
    return UpdatePlanOut(plan=args.plan, todo=args.todo, note=args.note, job_id=ctx.job_id,
                         step=ctx.step)


def _plan_dry(ctx: ToolContext, args: UpdatePlanIn, out: UpdatePlanOut) -> str:
    return f"Would save a plan of {len(out.plan)} steps and {len(out.todo)} to-dos"


def register_builtins(registry: ToolRegistry) -> None:
    registry.tool(kind="write_intent", scope="org:owner", tier="auto", cost_tier="knowledge",
                  idem=("channel", "shop_id", "body_digest", "date"), dry_run=_send_dry,
                  scope_fn=_send_scope)(send_to_user)
    registry.tool(kind="write_intent", scope="org:owner", tier="auto", cost_tier="ask_owner",
                  idem=("to", "shop_id", "question_digest", "date"), dry_run=_ask_dry,
                  scope_fn=_ask_scope)(ask_question)
    registry.tool(kind="write_intent", scope="org:plan", tier="auto", cost_tier="knowledge",
                  idem=("job_id", "step"), dry_run=_plan_dry)(update_plan)
