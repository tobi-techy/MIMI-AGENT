"""The computer-task service: the five endpoints' worth of behaviour.

It proposes a plan in code, runs an approved plan on the browser container in a
background thread (so the console can poll the steps as they appear), stops it
on request, and reads it back. Every row is filtered by the caller's tenant.

The engine is chosen once, from configuration, and never by a model. With no
container configured a task can still be proposed, and approving it fails with
a plain message rather than pretending to run.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mimi.clock import wall
from mimi.computer import plans
from mimi.computer.engine import (
    ComputerPort,
    EngineError,
    FakeComputer,
)
from mimi.computer.runner import Runner
from mimi.computer.store import ComputerStore, plan_dicts, plan_view, task_view
from mimi.computer.types import OPEN_STATUSES, GuardLimits, Step
from mimi.store.util import loads, now_iso

log = logging.getLogger(__name__)

MAX_GOAL = 600
MAX_INPUTS = 20

# The statuses a task is started from, and the ones it never moves out of.
RUNNABLE: tuple[str, ...] = ("proposed", "awaiting_owner")
CLOSED_STATUSES: tuple[str, ...] = ("done", "stopped", "stopped_runaway", "failed")


class Refused(Exception):
    """The request cannot be honoured, with a plain reason for the caller."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class _Run:
    """One task's run in flight: the owner's stop flag, and the runner once it
    exists. Registered the moment the row says "running", so a stop that lands
    before the thread starts is not lost."""

    stop: threading.Event
    runner: Runner | None = None


def engine_from_env() -> ComputerPort:
    """The configured browser container, or an unconfigured port.

    The provider is behind ``mimi.computer.cloud_browser``: with
    ``MIMI_CLOUD_BROWSER_API_KEY`` set, tasks run on a real cloud browser.
    ``MIMI_COMPUTER_ENGINE=maritime`` selects a Maritime Agent box instead.
    With nothing configured a task can still be proposed, and approving it
    says so plainly rather than pretending to run.
    """
    import os

    which = os.environ.get("MIMI_COMPUTER_ENGINE", "").strip()
    if which in ("", "cloud", "browserbase"):
        from mimi.computer.cloud_browser import engine_from_env as cloud

        engine = cloud()
        if engine.name != "unconfigured":
            return engine
    if which == "maritime":
        from mimi.computer.engine import MaritimeBoxBrowser, MaritimeConfig

        cfg = MaritimeConfig(
            token=os.environ.get("MIMI_MARITIME_TOKEN", "").strip(),
            agent_id=os.environ.get("MIMI_MARITIME_AGENT_ID", "").strip(),
            base=os.environ.get("MIMI_MARITIME_BASE", "").strip()
            or "https://api.maritime.sh")
        if cfg.usable:
            return MaritimeBoxBrowser(cfg)
    if which == "desktop":
        from mimi.computer.engine import MaritimeConfig, MaritimeDesktopBrowser

        return MaritimeDesktopBrowser(MaritimeConfig(
            token=os.environ.get("MIMI_MARITIME_TOKEN", "").strip()))
    return Unconfigured()


class Unconfigured:
    """No container is configured: proposing works, running says so plainly."""

    name = "unconfigured"

    def start(self, task: Any) -> None:
        raise EngineError("no browser container is configured")

    def do(self, task: Any, action: str, **kw: Any) -> Any:
        raise EngineError("no browser container is configured")

    def sign_in_link(self, task: Any) -> str:
        return ""

    def stop(self, task: Any, reason: str) -> None:
        return None


class ComputerService:
    def __init__(self, store: ComputerStore, engine: ComputerPort | None = None,
                 limits: GuardLimits | None = None,
                 clock: Callable[[], float] = wall) -> None:
        self.store = store
        self.engine = engine if engine is not None else engine_from_env()
        self.limits = limits or GuardLimits()
        self.clock = clock
        self._runs: dict[tuple[str, str], _Run] = {}
        self._live: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    # -- propose -----------------------------------------------------------
    def propose(self, tenant: str, goal: str, url: str, domain: str,
                kind: str = "generic", inputs: dict[str, str] | None = None
                ) -> dict[str, Any]:
        goal = (goal or "").strip()
        url = (url or "").strip()
        domain = (domain or "").strip().lower()
        if not goal:
            raise Refused("a task needs a goal")
        if len(goal) > MAX_GOAL:
            raise Refused("the goal is too long")
        if not url or not domain:
            raise Refused("a task needs a url and the domain it may use")
        from mimi.computer.guardrails import host_allowed

        if not host_allowed(url, domain):
            raise Refused("the url is not on the domain you approved")
        clean_inputs = {str(k)[:120]: str(v)[:500]
                        for k, v in list((inputs or {}).items())[:MAX_INPUTS]}
        plan = plans.build(kind, goal, url, domain, clean_inputs)
        if plan.kind == "gov_form":
            self._check_form(clean_inputs)
        task_id = f"ct_{uuid.uuid4().hex[:20]}"
        self.store.create(tenant, task_id, goal, url, domain, plan.kind,
                          plan_dicts(plan.steps), clean_inputs,
                          self.limits.model_dump(), self.engine.name)
        return {"task_id": task_id, "status": "proposed",
                "plan": plan_view(plan_dicts(plan.steps))}

    def _check_form(self, inputs: dict[str, str]) -> None:
        """A form task is filled from the owner's own named values, or refused.

        The labels and the values are the owner's words; nothing here is typed
        on the owner's behalf that the code refuses to type at all.
        """
        from mimi.computer.guardrails import credential_field, looks_like_secret

        if not inputs:
            raise Refused("a form task needs the values to fill: name each field "
                          "and give its value")
        for name, value in inputs.items():
            if credential_field(name):
                raise Refused(f"a form task never types a credential, and {name} "
                              "is a field a credential goes in")
            if looks_like_secret(value):
                raise Refused(f"a form task never types a long number, and the "
                              f"value for {name} looks like one")

    # -- read --------------------------------------------------------------
    def tasks(self, tenant: str) -> list[dict[str, Any]]:
        out = []
        for task in self.store.tasks(tenant):
            out.append({
                "task_id": task["task_id"],
                "goal": task["goal"],
                "domain": task["domain"],
                "status": task["status"],
                "created_at": task["created_at"],
                "step_count": int(task.get("step_count", 0)),
                "stop_reason": task.get("stop_reason", ""),
                # The sentence for a person, so the list obeys the same rule as
                # the detail: render this, never the code above.
                "note": task.get("note", ""),
            })
        return out

    def get(self, tenant: str, task_id: str) -> dict[str, Any] | None:
        task = self.store.get(tenant, task_id)
        if task is None:
            return None
        return task_view(task, self.store.steps(tenant, task_id),
                         self.store.artifacts(tenant, task_id),
                         self.live_link(tenant, task_id))

    def live_link(self, tenant: str, task_id: str) -> str:
        """The signed link to watch this task's session, while one is open."""
        with self._lock:
            return self._live.get((tenant, task_id), "")

    def kinds(self) -> list[dict[str, str]]:
        """The task kinds, exactly as the code names them, for the console."""
        return [{"kind": kind, "label": plans.KIND_LABEL.get(kind, "")}
                for kind in plans.PROFILES]

    def artifact(self, tenant: str, task_id: str, name: str) -> dict[str, Any] | None:
        return self.store.artifact(tenant, task_id, name)

    # -- run ---------------------------------------------------------------
    def approve(self, tenant: str, task_id: str, *, inline: bool = False
                ) -> dict[str, Any]:
        task = self.store.get(tenant, task_id)
        if task is None:
            raise Refused("no such task", 404)
        if task["status"] not in RUNNABLE:
            raise Refused(f"this task is {task['status']} and cannot be started")
        if self.engine.name == "unconfigured":
            raise Refused("no browser container is configured on this server", 503)
        # One conditional update, so two approvals in flight cannot both start
        # a run: the loser is told, not silently ignored.
        if not self.store.claim(tenant, task_id, RUNNABLE):
            raise Refused("this task has already been started", 409)
        # The stop flag exists from the moment the row says "running", so a stop
        # landing before the thread starts is not lost. The route drops any live
        # link the console holds, because this run will read its own.
        run = _Run(stop=threading.Event())
        with self._lock:
            self._runs[(tenant, task_id)] = run
            self._live.pop((tenant, task_id), None)
        if inline:
            self._run(tenant, task, run)
        else:
            threading.Thread(target=self._run, args=(tenant, task, run), daemon=True,
                             name=f"computer-{task_id}").start()
        return self.get(tenant, task_id) or {}

    def _run(self, tenant: str, task: dict[str, Any], run: _Run) -> None:
        task_id = task["task_id"]
        stop = run.stop
        outcome: Any = None
        try:
            limits = GuardLimits(**loads(task.get("limits"), {}))
            inputs = loads(task.get("inputs"), {})
            steps = [Step(**s) for s in loads(task.get("plan"), [])]

            def on_step(record: Any, blob: Any) -> None:
                self._read_live(tenant, task_id, task)
                self.store.add_step(tenant, task_id, record)
                if blob is not None:
                    self.store.add_artifact(tenant, task_id, blob)

            def on_artifact(blob: Any) -> None:
                self.store.add_artifact(tenant, task_id, blob)

            runner = Runner(self.engine, limits=limits, clock=self.clock,
                            on_step=on_step, on_artifact=on_artifact,
                            should_stop=stop.is_set)
            run.runner = runner
            # A resumed task continues the step log it paused with.
            outcome = runner.run(task_id, task["domain"], task["kind"], steps, inputs,
                                 start_index=self.store.next_index(tenant, task_id))
        except Exception as exc:  # never lose the task: report, do not crash
            log.warning("computer task %s failed: %s", task_id, exc)
            self._release(tenant, task_id, task)
            self.store.set_status(tenant, task_id, "failed", stop_reason="engine_error",
                                  note="The browser could not finish this task.",
                                  finished=True, only_from=("running",))
            return
        finally:
            # The run is over once its own row is written, below: until then a
            # stop still finds this flag, and the row it writes stands.
            if outcome is not None:
                if outcome.status != "awaiting_owner":
                    # The session's work is done. A task paused at a sign-in wall
                    # keeps its session and its live view link: that is where the
                    # owner signs in. This thread owns the session, so this is
                    # also the thread that closes it.
                    self._release(
                        tenant, task_id, task,
                        "stopped_by_owner"
                        if outcome.stop_reason == "stopped_by_owner" else "task_finished")
                # Only from "running": a stop that landed first wrote its own row,
                # and this run must not overwrite what the owner decided.
                self.store.set_status(tenant, task_id, outcome.status,
                                      stop_reason=outcome.stop_reason or "",
                                      note=outcome.note or "",
                                      finished=outcome.status != "awaiting_owner",
                                      only_from=("running",))
            with self._lock:
                self._runs.pop((tenant, task_id), None)

    def _read_live(self, tenant: str, task_id: str, task: dict[str, Any]) -> None:
        """Read the session's signed live view link once, while it is open, so
        the console can show the owner the real screen. Never stored."""
        if self.live_link(tenant, task_id):
            return
        from mimi.computer.engine import TaskRef

        try:
            link = self.engine.sign_in_link(
                TaskRef(task_id=task_id, domain=task.get("domain", "")))
        except EngineError:
            return
        if link:
            with self._lock:
                self._live[(tenant, task_id)] = link

    def _release(self, tenant: str, task_id: str, task: dict[str, Any],
                 reason: str = "task_finished") -> None:
        """Close the browser session, if one is still open, and forget its link."""
        from mimi.computer.engine import TaskRef

        try:
            self.engine.stop(TaskRef(task_id=task_id, domain=task.get("domain", "")),
                             reason)
        except EngineError:
            pass
        with self._lock:
            self._live.pop((tenant, task_id), None)

    # -- stop --------------------------------------------------------------
    def stop(self, tenant: str, task_id: str) -> dict[str, Any]:
        task = self.store.get(tenant, task_id)
        if task is None:
            raise Refused("no such task", 404)
        if task["status"] in CLOSED_STATUSES:
            return self.get(tenant, task_id) or {}

        with self._lock:
            run = self._runs.get((tenant, task_id))
            if run is not None:
                run.stop.set()  # the runner stops at its next step, keeping nothing
        # The status is written before the session is closed: the run's own or
        # failure writes are conditional on "running", so the owner's stop is
        # the row that stands.
        self.store.set_status(tenant, task_id, "stopped",
                              stop_reason="stopped_by_owner",
                              note="Stopped by the owner.", finished=True,
                              only_from=OPEN_STATUSES)
        if run is None:
            # No run in flight, so nothing else will close this session. When a
            # run is in flight it owns the session and closes it on its own
            # thread as it returns.
            self._release(tenant, task_id, task, "stopped_by_owner")
        return self.get(tenant, task_id) or {}


def fake_service(engine: FakeComputer | None = None, store: ComputerStore | None = None,
                 limits: GuardLimits | None = None) -> ComputerService:
    """A service over the offline fake, for the tests."""
    if store is None:
        from mimi.store.db import ensure_schema, make_engine

        db = make_engine("sqlite://")
        ensure_schema(db)
        store = ComputerStore(db, artifact_dir=_tmp())
    return ComputerService(store, engine or FakeComputer(), limits)


def _tmp() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="mimi-computer-test-")


def now() -> str:
    return now_iso()
