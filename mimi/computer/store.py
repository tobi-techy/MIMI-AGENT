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


# The newest tasks the list endpoint serves. A task row is small, but the list
# is not a log: the console shows the recent ones.
MAX_TASKS = 200


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

    def set_status(self, tenant: str, task_id: str, status: str,
                   *, stop_reason: str | None = None, note: str | None = None,
                   finished: bool = False,
                   only_from: tuple[str, ...] | None = None) -> bool:
        """Move a task to ``status`` and say whether the row moved.

        ``stop_reason`` and ``note``: ``None`` leaves the stored value alone,
        the empty string clears it, anything else writes it. ``only_from``
        makes the write conditional on the status the row still has, so a
        finished or stopped task can never be overwritten by a late write.
        """
        values: dict[str, Any] = {"status": status}
        if stop_reason is not None:
            values["stop_reason"] = stop_reason
        if note is not None:
            values["note"] = note
        if finished:
            values["finished_at"] = now_iso()
        where = [s.computer_tasks.c.tenant_id == tenant,
                 s.computer_tasks.c.task_id == task_id]
        if only_from is not None:
            where.append(s.computer_tasks.c.status.in_(list(only_from)))
        with self._conn() as conn:
            res = conn.execute(sa.update(s.computer_tasks).where(*where).values(**values))
            return bool(res.rowcount == 1)

    def claim(self, tenant: str, task_id: str,
              statuses: tuple[str, ...] = ("proposed",)) -> bool:
        """Start a task, but only from one of ``statuses``.

        One conditional UPDATE, so two approvals in flight cannot both start a
        run: the loser gets False. ``started_at`` is stamped the first time the
        task runs and kept after that. The reason and the note of an earlier
        run are cleared here.
        """
        with self._conn() as conn:
            res = conn.execute(sa.update(s.computer_tasks).where(
                s.computer_tasks.c.tenant_id == tenant,
                s.computer_tasks.c.task_id == task_id,
                s.computer_tasks.c.status.in_(list(statuses))).values(
                status="running", stop_reason="", note="",
                started_at=sa.case((s.computer_tasks.c.started_at == "", now_iso()),
                                   else_=s.computer_tasks.c.started_at)))
            return bool(res.rowcount == 1)

    def get(self, tenant: str, task_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(sa.select(s.computer_tasks).where(
                s.computer_tasks.c.tenant_id == tenant,
                s.computer_tasks.c.task_id == task_id)).first()
        return _row(row) if row is not None else None

    def tasks(self, tenant: str, limit: int = MAX_TASKS) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(sa.select(s.computer_tasks).where(
                s.computer_tasks.c.tenant_id == tenant).order_by(
                s.computer_tasks.c.created_ts.desc(), s.computer_tasks.c.created_at.desc()
            ).limit(limit)
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
            # The list renders the sentence, like the detail, never the code.
            d["note"] = _note(d)
            out.append(d)
        return out

    # -- steps and artifacts ----------------------------------------------
    def add_step(self, tenant: str, task_id: str, record: StepRecord) -> None:
        with self._conn() as conn:
            conn.execute(s.computer_steps.insert().values(
                tenant_id=tenant, task_id=task_id, idx=record.index,
                action=record.action, url=record.url, note=record.note,
                screenshot=record.screenshot, created_at=now_iso()))

    def next_index(self, tenant: str, task_id: str) -> int:
        """The next step-log index for a task, so a resumed run continues its
        own log rather than writing rows the paused run already wrote."""
        with self._conn() as conn:
            top = conn.execute(sa.select(sa.func.max(s.computer_steps.c.idx)).where(
                s.computer_steps.c.tenant_id == tenant,
                s.computer_steps.c.task_id == task_id)).scalar()
        return int(top) + 1 if top is not None else 0

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


def _note(task: dict[str, Any]) -> str:
    """The sentence for a person, which is the field the console renders.

    A task that has stopped always carries one: if a row somehow holds a stop
    reason with no note beside it, the code writes the sentence now rather than
    leaving the console to invent words or show the code.
    """
    note = str(task.get("note", "") or "")
    reason = str(task.get("stop_reason", "") or "")
    if note or not reason:
        return note
    from mimi.computer.runner import stop_note

    return stop_note(reason)


def task_view(task: dict[str, Any], steps: list[dict[str, Any]],
              artifacts: list[dict[str, Any]], live_view_url: str = "") -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "kind": task.get("kind", "generic"),
        "goal": task["goal"],
        "url": task["url"],
        "domain": task["domain"],
        # A machine code, never rendered on screen, and the sentence beside it.
        "stop_reason": task.get("stop_reason", ""),
        "note": _note(task),
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
