"""The runner: executes one approved plan under the guardrails, step by step.

Everything the brief fixes in code lives here:

* the allowlist is checked before a navigation and against what was observed
  after it, and a mismatch ends the task ``off_domain``;
* a login wall pauses the task ``awaiting_owner`` and nothing is typed;
* a checkout, a card field, a message box or a settings page ends it
  ``forbidden_page``;
* a step outside the kind's profile is refused ``unsupported_step``;
* the budgets (10 minutes, 40 steps by default) end it ``stopped_runaway``;
* the owner's stop is read between steps and ends it ``stopped_by_owner``, and
  nothing the in-flight step produced is kept;
* a form is filled only from the owner's own inputs, never a credential;
* every step is logged with a redacted note and a screenshot artifact, and what
  a step produced (the text a read gave, the file a download gave) is kept as
  its own artifact for the owner to review.

The runner never calls a model and never chooses an action: it runs the plan
the owner approved.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from mimi.clock import wall
from mimi.computer import guardrails as g
from mimi.computer import plans
from mimi.computer.engine import ComputerPort, EngineError, TaskRef
from mimi.computer.types import Artifact, GuardLimits, Step, StepRecord, TaskStatus

MAX_NOTE = 300


def _never() -> bool:
    return False


@dataclass
class Blob:
    name: str
    kind: Literal["screenshot", "text", "file"]
    mime: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class Outcome:
    status: TaskStatus = "done"
    stop_reason: str = ""
    note: str = ""
    steps: list[StepRecord] = field(default_factory=list)
    artifacts: list[Blob] = field(default_factory=list)

    def artifact_index(self) -> list[Artifact]:
        return [Artifact(name=b.name, kind=b.kind, mime=b.mime, size=len(b.data),
                         sha256=b.sha256) for b in self.artifacts]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower())[:60] or "item"


_DOWNLOAD_MIME = {
    "pdf": "application/pdf", "csv": "text/csv", "txt": "text/plain",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel", "zip": "application/zip",
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
}


def mime_for(name: str) -> str:
    """The mime type of a downloaded file, from the name the site gave it."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _DOWNLOAD_MIME.get(ext, "application/octet-stream")


class Runner:
    """Runs one plan. ``on_step`` is called after each recorded step so a caller
    can persist and let the console poll; ``on_artifact`` is called with every
    result blob (a screenshot, the text a step read, a downloaded file) so the
    caller persists that too."""

    def __init__(self, port: ComputerPort, limits: GuardLimits | None = None,
                 clock: Callable[[], float] = wall,
                 on_step: Callable[[StepRecord, Blob | None], None] | None = None,
                 on_artifact: Callable[[Blob], None] | None = None,
                 should_stop: Callable[[], bool] | None = None):
        self.port = port
        self.limits = limits or GuardLimits()
        self.clock = clock
        self.on_step = on_step
        self.on_artifact = on_artifact
        # The owner's stop, read between steps: the caller sets its own flag
        # when ``/stop`` arrives, so a stopped task stops doing things.
        self.should_stop = should_stop or _never

    def stopped(self) -> bool:
        """Whether the owner asked to stop, this instant."""
        try:
            return bool(self.should_stop())
        except Exception:  # a broken flag never runs a task on: stop instead
            return True

    def run(self, task_id: str, domain: str, kind: str, steps: list[Step],
            inputs: dict[str, str] | None = None, start_index: int = 0) -> Outcome:
        inputs = dict(inputs or {})
        profile = plans.profile_for(kind)
        ref = TaskRef(task_id=task_id, domain=domain)
        out = Outcome()
        current_url = ""
        try:
            self.port.start(ref)
        except EngineError as exc:
            return self._fail(out, "failed", f"The browser did not start: {exc}")

        started = self.clock()
        # A resumed task carries on the step log it already has, so its rows can
        # never collide with the rows the paused run wrote. The step budget
        # counts the task's whole log, so no number of resumes can push a task
        # past it.
        index = start_index
        for step in steps:
            if self.stopped():
                return self._stop(out, "stopped", "stopped_by_owner")
            if step.action == "finish":
                rec = StepRecord(index=index, action="finish", url=current_url,
                                 note=g.redact(step.note))
                out.steps.append(rec)
                if self.on_step is not None:
                    self.on_step(rec, None)
                break
            stop = g.budget_stop(index, self.clock() - started, self.limits)
            if stop:
                return self._stop(out, "stopped_runaway", stop)
            refusal = self._precheck(profile, step, domain, inputs)
            if refusal is not None:
                return self._stop(out, refusal[0], refusal[1])
            value = inputs.get(step.value_key, "") if step.action == "fill" else ""
            try:
                page = self.port.do(ref, step.action, url=step.url, target=step.target,
                                    field=step.field, value=value,
                                    direction=step.direction, amount=step.amount,
                                    current_url=current_url)
            except EngineError as exc:
                return self._fail(out, "failed",
                                  f"The browser could not do that step: {exc}")
            if self.stopped():
                # The stop landed while that step was in flight: nothing it
                # produced is recorded or kept, and the caller's own record of
                # the stop stands.
                return self._stop(out, "stopped", "stopped_by_owner")
            if page.url:
                current_url = page.url
            # The allowlist is the first verdict on the observed URL, before
            # the page is judged at all: a redirect, or a page that moves
            # itself, to a domain the owner never approved ends the task
            # without reading or keeping anything from it.
            if page.url and not g.host_allowed(page.url, domain):
                return self._stop(out, "stopped", "off_domain")
            verdict = g.classify(page)
            if verdict.kind == "forbidden":
                blob = self._screenshot(out, index, page.screenshot)
                self._record(out, index, step, page, blob, "A page we never act on")
                return self._stop(out, "stopped", "forbidden_page")
            if verdict.kind == "login_wall":
                blob = self._screenshot(out, index, page.screenshot)
                self._record(out, index, step, page, blob, "This page asks for a sign-in")
                out.status = "awaiting_owner"
                out.stop_reason = "login_wall"
                out.note = ("This page asks for a sign-in. Mimi never types a "
                            "password or a code, so the task is paused here.")
                return out
            blob = self._body_artifact(out, index, step, page)
            self._record(out, index, step, page, blob, "")
            index += 1
        else:
            return self._fail(out, "failed", "the plan had no finish step")
        out.status = "done"
        out.note = "Done. The results are artifacts you can review."
        return out

    # -- helpers -----------------------------------------------------------
    def _precheck(self, profile: plans.Profile, step: Step, domain: str,
                  inputs: dict[str, str]) -> tuple[TaskStatus, str] | None:
        action = step.action
        if action not in profile.actions:
            if action == "fill" and not profile.may_fill:
                return ("stopped", "unsupported_step")
            if action == "download" and not profile.may_download:
                return ("stopped", "unsupported_step")
            return ("stopped", "unsupported_step")
        if action == "goto":
            if not g.host_allowed(step.url, domain):
                return ("stopped", "off_domain")
        if action == "fill":
            if g.credential_field(step.field):
                return ("stopped", "forbidden_page")
            value = inputs.get(step.value_key, "")
            if not value:
                return ("stopped", "unsupported_step")
            if g.looks_like_secret(value):
                return ("stopped", "forbidden_page")
        return None

    def _screenshot(self, out: Outcome, index: int, data: bytes) -> Blob | None:
        if not data:
            return None
        blob = Blob(name=f"step-{index:02d}.png", kind="screenshot",
                    mime="image/png", data=data)
        out.artifacts.append(blob)  # handed to the caller with its step record
        return blob

    def _keep(self, out: Outcome, blob: Blob) -> None:
        """Keep a result blob that is not the step's screenshot (the text a read
        gave, the file a download gave) and hand it to the caller to persist."""
        out.artifacts.append(blob)
        if self.on_artifact is not None:
            self.on_artifact(blob)

    def _body_artifact(self, out: Outcome, index: int, step: Step,
                       page: g.Page) -> Blob | None:
        """The result of one step: the file a download gave, the text a read
        gave, and the screenshot that goes with the step."""
        if step.action == "download":
            if not page.screenshot:
                return None
            name = _slug(page.filename or step.target or f"download-{index:02d}")
            self._keep(out, Blob(name=name, kind="file",
                                 mime=mime_for(name), data=page.screenshot))
            return None
        shot = self._screenshot(out, index, page.screenshot)
        if step.action == "extract" and page.text:
            name = f"{_slug(step.target or 'page')}-{index:02d}.txt"
            self._keep(out, Blob(name=name, kind="text", mime="text/plain",
                                 data=page.text.encode()))
        return shot

    def _record(self, out: Outcome, index: int, step: Step, page: g.Page,
                blob: Blob | None, extra: str) -> None:
        note = g.note_for(step.action, step.note or extra, page)
        if extra and extra not in note:
            note = g.redact(f"{extra}: {note}")[:MAX_NOTE]
        rec = StepRecord(index=index, action=step.action, url=page.url or step.url,
                         note=note[:MAX_NOTE], screenshot=blob.name if blob else "")
        out.steps.append(rec)
        if self.on_step is not None:
            self.on_step(rec, blob)

    def _stop(self, out: Outcome, status: TaskStatus, reason: str) -> Outcome:
        out.status = status
        out.stop_reason = reason
        # ``stop_reason`` is the machine code; the sentence is always here, so
        # the console never has to invent words for it or show a code.
        out.note = stop_note(reason)
        return out

    def _fail(self, out: Outcome, status: TaskStatus, note: str) -> Outcome:
        out.status = status
        out.stop_reason = "engine_error"
        out.note = note
        return out


STOP_NOTES: dict[str, str] = {
    "off_domain": ("Stopped: the browser left the domain you approved. Nothing "
                   "after that was read or kept."),
    "forbidden_page": ("Stopped: the page was a checkout, a card form or a "
                       "messaging page. Mimi never buys, pays, sends or changes "
                       "settings."),
    "budget_steps": ("Stopped by the step budget. Nothing more was done after that."),
    "budget_seconds": ("Stopped by the time budget. Nothing more was done after that."),
    "unsupported_step": ("Stopped: the plan held a step this kind of task may not "
                         "run."),
    "stopped_by_owner": "Stopped by the owner.",
    "engine_error": "Stopped: the browser could not do that step.",
    "login_wall": ("Stopped: this page asks for a sign-in. Mimi never types a "
                   "password or a code."),
}

# For a stop reason with no entry of its own. The console renders ``note``, so
# it must never be empty and never be a machine code, whatever reason arrives.
DEFAULT_STOP_NOTE = "Stopped. Nothing more was done after that."


def stop_note(reason: str) -> str:
    """The sentence a person reads for one stop reason. Never a code."""
    return STOP_NOTES.get(reason, DEFAULT_STOP_NOTE)
