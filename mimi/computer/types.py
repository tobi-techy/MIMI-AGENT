"""The vocabulary the computer-task runner and the API share.

A task is a goal, an approved domain, and a plan of steps. The plan is code
here, never a model: the owner approves the plan once and the runner executes
exactly those steps, re-checking the guardrails on every one.

Money never appears in a step. Times come from ``mimi.clock``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

# The step's own limits, so a plan built in code cannot carry a runaway value.
Short = Annotated[str, StringConstraints(max_length=300)]
Long = Annotated[str, StringConstraints(max_length=2000)]
Name = Annotated[str, StringConstraints(max_length=120)]
Small = Annotated[str, StringConstraints(max_length=16)]

TaskKind = Literal["price_check", "order_list", "bank_statement", "gov_form", "generic"]
TaskStatus = Literal["proposed", "running", "awaiting_owner", "done", "stopped",
                     "stopped_runaway", "failed"]
StepAction = Literal["goto", "scroll", "extract", "screenshot", "click", "fill",
                     "download", "finish"]
StopReason = Literal["", "off_domain", "forbidden_page", "login_wall", "budget_steps",
                     "budget_seconds", "stopped_by_owner", "engine_error",
                     "unsupported_step"]

KINDS: tuple[TaskKind, ...] = ("price_check", "order_list", "bank_statement",
                               "gov_form", "generic")

OPEN_STATUSES: tuple[TaskStatus, ...] = ("proposed", "running", "awaiting_owner")


class Step(BaseModel):
    """One planned step. ``note`` is what the owner reads in the proposal."""

    model_config = ConfigDict(extra="forbid")

    action: StepAction
    note: Short = ""
    url: Long = ""
    target: Short = ""  # the text to click or read
    field: Name = ""  # the form field, by its label
    value_key: Name = ""  # a key into the task's own inputs
    direction: Small = ""
    amount: int = 0


class GuardLimits(BaseModel):
    """The budgets, in code. Exceeding either ends the task ``stopped_runaway``."""

    model_config = ConfigDict(extra="forbid")

    max_steps: int = 40
    max_seconds: float = 600.0


class Artifact(BaseModel):
    """A file a task produced (a screenshot or an extracted text)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["screenshot", "text", "file"]
    mime: str = "text/plain"
    size: int = 0
    sha256: str = ""


class StepRecord(BaseModel):
    """One line of the step log, as the console reads it."""

    model_config = ConfigDict(extra="forbid")

    index: int
    action: str
    url: str = ""
    note: str = ""
    screenshot: str = ""  # the artifact name, served by the artifacts route
