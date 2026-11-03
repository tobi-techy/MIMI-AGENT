"""Computer tasks in the store: the task, its steps and its artifacts.

Every row carries ``tenant_id`` and every key starts with it, like the rest of
the store. No credential, no card number and no page text is ever stored: a
step holds a redacted note and the name of a screenshot artifact.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.clock import wall
from mimi.computer import plans
from mimi.computer.runner import Blob
from mimi.computer.types import Artifact, StepRecord
from mimi.store import schema as s
from mimi.store.util import dumps, loads, now_iso


def _row(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


class ComputerStore:
    """The persistence for computer tasks, over the API store's engine."""

    def __init__(self, engine: Engine, artifact_dir: str = "") -> None:
        self.engine = engine
        self.dir = Path(artifact_dir) if artifact_dir else Path(
            os.environ.get("MIMI_COMPUTER_DIR", "") or _default_dir())

    # -- tasks -------------------------------------------------------------
    def create(self, tenant: str, task_id: str, goal: str, url: str, domain: str,
               kind: str, plan: list[dict[str, Any]], inputs: dict[str, str],
               limits: dict[str, Any], engine_name: str) -> dict[str, Any]:
        with self._conn() as conn:
            conn.execute(s.computer_tasks.insert().values(
                tenant_id=tenant, task_id=task_id, goal=goal, url=url, domain=domain,
                kind=kind, status="proposed", plan=dumps(plan), inputs=dumps(inputs),
                limits=dumps(limits), engine=engine_name, stop_reason="", note="",
                created_at=now_iso(), created_ts=wall()))
        return self.get(tenant, task_id) or {}

    def set_status(self, tenant: str, task_id: str, status: str, *, stop_reason: str = "",
                   note: str = "", finished: bool = False) -> None:
        values: dict[str, Any] = {"status": status}
        if stop_reason:
            values["stop_reason"] = stop_reason
        if note:
            values["note"] = note
        if finished:
            values["finished_at"] = now_iso()
        with self._conn() as conn:
            conn.execute(sa.update(s.computer_tasks).where(
                s.computer_tasks.c.tenant_id == tenant,
                s.computer_tasks.c.task_id == task_id).values(**values))

    def get(self, tenant: str, task_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(sa.select(s.computer_tasks).where(
                s.computer_tasks.c.tenant_id == tenant,
                s.computer_tasks.c.task_id == task_id)).first()
        return _row(row) if row is not None else None

    def tasks(self, tenant: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(sa.select(s.computer_tasks).where(
                s.computer_tasks.c.tenant_id == tenant).order_by(
                s.computer_tasks.c.created_ts.desc(), s.computer_tasks.c.created_at.desc())
            ).all()
            counted = conn.execute(
                sa.select(s.computer_steps.c.task_id, sa.func.count()).where(
                    s.computer_steps.c.tenant_id == tenant).group_by(
                    s.computer_steps.c.task_id)).all()
        counts: dict[str, int] = {str(row[0]): int(row[1]) for row in counted}
        out = []
        for row in rows:
            d = _row(row)
            d["step_count"] = int(counts.get(d["task_id"], 0))
            out.append(d)
        return out

    # -- steps and artifacts ----------------------------------------------
    def add_step(self, tenant: str, task_id: str, record: StepRecord) -> None:
        with self._conn() as conn:
            conn.execute(s.computer_steps.insert().values(
                tenant_id=tenant, task_id=task_id, idx=record.index,
                action=record.action, url=record.url, note=record.note,
                screenshot=record.screenshot, created_at=now_iso()))

    def steps(self, tenant: str, task_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(sa.select(s.computer_steps).where(
                s.computer_steps.c.tenant_id == tenant,
                s.computer_steps.c.task_id == task_id).order_by(
                s.computer_steps.c.idx)).all()
        return [_row(r) for r in rows]

    def add_artifact(self, tenant: str, task_id: str, blob: Blob) -> Artifact:
        path = self.dir / tenant / task_id
        path.mkdir(parents=True, exist_ok=True)
        target = path / blob.name
        target.write_bytes(blob.data)
        art = Artifact(name=blob.name, kind=blob.kind, mime=blob.mime,
                       size=len(blob.data), sha256=blob.sha256)
        with self._conn() as conn:
            conn.execute(s.computer_artifacts.insert().values(
                tenant_id=tenant, task_id=task_id, name=art.name, kind=art.kind,
                mime=art.mime, size=art.size, sha256=art.sha256,
                path=str(target), created_at=now_iso()))
        return art

    def artifacts(self, tenant: str, task_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(sa.select(s.computer_artifacts).where(
                s.computer_artifacts.c.tenant_id == tenant,
                s.computer_artifacts.c.task_id == task_id).order_by(
                s.computer_artifacts.c.name)).all()
        return [_row(r) for r in rows]

    def artifact(self, tenant: str, task_id: str, name: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(sa.select(s.computer_artifacts).where(
                s.computer_artifacts.c.tenant_id == tenant,
                s.computer_artifacts.c.task_id == task_id,
                s.computer_artifacts.c.name == name)).first()
        return _row(row) if row is not None else None

    # -- plumbing ----------------------------------------------------------
    @contextmanager
    def _conn(self) -> Iterator[sa.Connection]:
        with self.engine.begin() as conn:
            yield conn


def _default_dir() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="mimi-computer-")


def plan_dicts(steps: list[Any]) -> list[dict[str, Any]]:
    """The plan as it is stored and run: the machine verb is ``action``."""
    return [step.model_dump() for step in steps]


def step_view(row: dict[str, Any], index_key: str = "idx") -> dict[str, Any]:
    """One step as the console reads it.

    ``action`` is the plain words and the machine verb moves to ``code``, which
    the console does not render: a token like ``goto`` must never be able to
    reach a distributor's screen.
    """
    verb = str(row.get("action", ""))
    return {
        "index": row.get(index_key, row.get("index")),
        "action": plans.plain(verb),
        "code": verb,
        "url": row.get("url", ""),
        "note": row.get("note", ""),
        "screenshot": row.get("screenshot", ""),
    }


def plan_view(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A stored or freshly built plan, rendered the same way as a logged step."""
    out: list[dict[str, Any]] = []
    for step in rows:
        row = dict(step)
        verb = str(row.get("action", ""))
        row["action"] = plans.plain(verb)
        row["code"] = verb
        out.append(row)
    return out


def task_view(task: dict[str, Any], steps: list[dict[str, Any]],
              artifacts: list[dict[str, Any]], live_view_url: str = "") -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "kind": task.get("kind", "generic"),
        "goal": task["goal"],
        "url": task["url"],
        "domain": task["domain"],
        "stop_reason": task.get("stop_reason", ""),
        "note": task.get("note", ""),
        "created_at": task["created_at"],
        "started_at": task.get("started_at", ""),
        "finished_at": task.get("finished_at", ""),
        # A signed link to watch this task's session, while one is open. It is
        # per session and dies with it, so it is never stored.
        "live_view_url": live_view_url or None,
        "plan": plan_view(loads(task.get("plan"), [])),
        "inputs": loads(task.get("inputs"), {}),
        "steps": [step_view(st) for st in steps],
        "artifacts": [{"name": a["name"], "kind": a["kind"], "mime": a["mime"],
                       "size": a["size"], "sha256": a["sha256"]} for a in artifacts],
    }
