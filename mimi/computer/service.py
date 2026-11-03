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
from typing import Any

from mimi.clock import wall
from mimi.computer import plans
from mimi.computer.engine import (
    ComputerPort,
    EngineError,
    FakeComputer,
)
from mimi.computer.runner import Runner
from mimi.computer.store import ComputerStore, plan_dicts, task_view
from mimi.computer.types import GuardLimits, Step
from mimi.store.util import loads, now_iso

log = logging.getLogger(__name__)

MAX_GOAL = 600
MAX_INPUTS = 20


class Refused(Exception):
    """The request cannot be honoured, with a plain reason for the caller."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


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
        self._runs: dict[tuple[str, str], Runner] = {}
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
        plan = plans.build(kind, goal, url, domain)
        task_id = f"ct_{uuid.uuid4().hex[:20]}"
        self.store.create(tenant, task_id, goal, url, domain, plan.kind,
                          plan_dicts(plan.steps), clean_inputs,
                          self.limits.model_dump(), self.engine.name)
        return {"task_id": task_id, "status": "proposed",
                "plan": plan_dicts(plan.steps)}

    # -- read --------------------------------------------------------------
    def list(self, tenant: str) -> list[dict[str, Any]]:
        out = []
        for task in self.store.list(tenant):
            out.append({
                "task_id": task["task_id"],
                "goal": task["goal"],
                "domain": task["domain"],
                "status": task["status"],
                "created_at": task["created_at"],
                "step_count": int(task.get("step_count", 0)),
                "stop_reason": task.get("stop_reason", ""),
            })
        return out

    def get(self, tenant: str, task_id: str) -> dict[str, Any] | None:
        task = self.store.get(tenant, task_id)
        if task is None:
            return None
        return task_view(task, self.store.steps(tenant, task_id),
                         self.store.artifacts(tenant, task_id))

    def artifact(self, tenant: str, task_id: str, name: str) -> dict[str, Any] | None:
        return self.store.artifact(tenant, task_id, name)

    # -- run ---------------------------------------------------------------
    def approve(self, tenant: str, task_id: str, *, inline: bool = False
                ) -> dict[str, Any]:
        task = self.store.get(tenant, task_id)
        if task is None:
            raise Refused("no such task", 404)
        if task["status"] not in ("proposed", "awaiting_owner"):
            raise Refused(f"this task is {task['status']} and cannot be started")
        if self.engine.name == "unconfigured":
            raise Refused("no browser container is configured on this server", 503)
        self.store.set_status(tenant, task_id, "running")
        if inline:
            self._run(tenant, task)
        else:
            threading.Thread(target=self._run, args=(tenant, task), daemon=True,
                             name=f"computer-{task_id}").start()
        return self.get(tenant, task_id) or {}

    def _run(self, tenant: str, task: dict[str, Any]) -> None:
        task_id = task["task_id"]
        limits = GuardLimits(**loads(task.get("limits"), {}))
        inputs = loads(task.get("inputs"), {})
        steps = [Step(**s) for s in loads(task.get("plan"), [])]

        def on_step(record: Any, blob: Any) -> None:
            self.store.add_step(tenant, task_id, record)
            if blob is not None:
                self.store.add_artifact(tenant, task_id, blob)

        def on_artifact(blob: Any) -> None:
            self.store.add_artifact(tenant, task_id, blob)

        runner = Runner(self.engine, limits=limits, clock=self.clock,
                        on_step=on_step, on_artifact=on_artifact)
        with self._lock:
            self._runs[(tenant, task_id)] = runner
        try:
            outcome = runner.run(task_id, task["domain"], task["kind"], steps, inputs)
        except Exception as exc:  # never lose the task: report, do not crash
            log.warning("computer task %s failed: %s", task_id, exc)
            self.store.set_status(tenant, task_id, "failed", stop_reason="engine_error",
                                  note="The browser could not finish this task.",
                                  finished=True)
            return
        self.store.set_status(tenant, task_id, outcome.status,
                              stop_reason=outcome.stop_reason, note=outcome.note,
                              finished=outcome.status != "awaiting_owner")

    # -- stop --------------------------------------------------------------
    def stop(self, tenant: str, task_id: str) -> dict[str, Any]:
        task = self.store.get(tenant, task_id)
        if task is None:
            raise Refused("no such task", 404)
        if task["status"] in ("done", "stopped", "stopped_runaway", "failed"):
            return task_view(task, self.store.steps(tenant, task_id),
                             self.store.artifacts(tenant, task_id))
        from mimi.computer.engine import TaskRef

        try:
            self.engine.stop(TaskRef(task_id=task_id, domain=task["domain"]),
                             "stopped_by_owner")
        except EngineError:
            pass
        self.store.set_status(tenant, task_id, "stopped", stop_reason="stopped_by_owner",
                              note="Stopped by the owner.", finished=True)
        return self.get(tenant, task_id) or {}


def fake_service(engine: FakeComputer | None = None, store: ComputerStore | None = None,
                 limits: GuardLimits | None = None) -> ComputerService:
    """A service over the offline fake, for the tests."""
    if store is None:
        from mimi.store.db import ensure_schema, make_engine

        engine = make_engine("sqlite://")
        ensure_schema(engine)
        store = ComputerStore(engine, artifact_dir=_tmp())
    return ComputerService(store, engine or FakeComputer(), limits)


def _tmp() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="mimi-computer-test-")


def now() -> str:
    return now_iso()
