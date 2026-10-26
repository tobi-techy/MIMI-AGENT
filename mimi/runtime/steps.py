"""What one step gets and what it must give back.

A step is one model or tool call. It may only return typed intents its helper
registered with ``outbox.intent_types``: those are proposals the system of
record validates and commits or puts to the owner. No intent type moves money, and the runner refuses any
object that is not one of them, so a step cannot smuggle a side effect out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

OutcomeKind = Literal["continue", "done", "ask_owner", "sleep", "blocked"]


@dataclass(frozen=True)
class StepContext:
    job_id: str
    tenant_id: str
    helper: str
    goal: str
    step_index: int
    plan: tuple[str, ...]
    todo: tuple[str, ...]
    working_note: str
    # Owner messages taken at this boundary come first: they steer the step.
    new_owner_messages: tuple[str, ...]
    now: datetime


@dataclass(frozen=True)
class StepOutcome:
    kind: OutcomeKind = "continue"
    # The helper's typed proposals, registered with ``outbox.intent_types``. The runner
    # checks every one before anything leaves; anything else blocks the job.
    intents: tuple[Any, ...] = ()
    # Model spend for this step in kobo, measured by the gateway client.
    cost_kobo: int = 0
    plan: tuple[str, ...] | None = None
    todo: tuple[str, ...] | None = None
    working_note: str | None = None
    question: str | None = None
    sleep_until: datetime | None = None
    result: str | None = None
    # Free-form trace attributes (tool name, model, tokens). Never figures the
    # owner will read: those come from intents built in code.
    trace: dict[str, str | int | float] = field(default_factory=dict)


class StepFn(Protocol):
    def __call__(self, ctx: StepContext) -> StepOutcome: ...
