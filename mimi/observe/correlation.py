"""One correlation id for everything one piece of work touches.

A shop message arrives at ``POST /v1/turns``, is queued as a turn, claimed by a
worker, judged by a helper, and may write to the book, draft a reply, ask the
owner for an approval or start a follow-up turn. Each of those hops writes a
different row. This module gives the whole chain **one id** and keeps it in
the log and the trace, so an operator can answer "what happened to this
message?" with one search instead of five joins.

The rules, all code:

* **A turn's trace id is its own turn id** (``turn_<digest>``, deterministic
  from the distributor and the provider's message id). A redelivered message
  gets the same turn, so it gets the same trace id: a retry is the same
  incident, not a new one. ``bind_turn`` also carries the attempt number, so
  attempt 2 of one trace is labelled ``attempt=2``.
* **Work a turn starts carries the turn's trace id**, not a new one: a
  follow-up turn for one part of a burst, a runtime job it delegates to, a
  connector write its approval applied. ``new_trace`` exists for work that
  starts with no inbound message (a connector sync, a maintenance job).
* **The id is never private.** It is a digest, not a phone number or a name.
  It is safe in a log line, a metric label and a trace attribute, and it
  passes ``mimi.observe.redact`` unchanged.

Nothing here imports a model, a store or the harness: the API, the worker,
the runtime and the evals all import this module, so it may import only the
standard library.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any

TOKEN = "trace"
ATTEMPT = "attempt"
# Anything that looks like a trace id (ours or a provider's W3C traceparent id)
# is kept by the log filter as a field and never re-written.
_TRACE_SHAPE = re.compile(r"^(?:turn|tr)_[0-9a-f]{20,32}$")


@dataclass(frozen=True)
class Context:
    """What the current piece of work is and which trace it belongs to."""

    trace: str = ""
    tenant: str = ""
    thread: str = ""
    turn: str = ""
    job: str = ""
    attempt: int = 0

    def fields(self) -> dict[str, Any]:
        """The log/trace fields, with empty values left out."""
        out: dict[str, Any] = {}
        for key in ("trace", "tenant", "thread", "turn", "job"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.attempt:
            out[ATTEMPT] = self.attempt
        return out


_EMPTY = Context()
_CURRENT: contextvars.ContextVar[Context] = contextvars.ContextVar(
    "mimi_correlation", default=_EMPTY)


def id_for(kind: str, *parts: str) -> str:
    """A deterministic id: the same work names the same id on every process."""
    raw = "\x00".join(p.strip() for p in parts)
    return f"{kind}_" + hashlib.sha256(f"{kind}\x00{raw}".encode()).hexdigest()[:20]


def turn_trace(turn_id: str) -> str:
    """A turn's trace id is the turn id itself (one id, no mapping table)."""
    return turn_id


def new_trace(kind: str = "work") -> str:
    """A trace id for work that starts with no inbound message."""
    return f"{kind}_" + uuid.uuid4().hex[:20]


def is_trace_id(value: object) -> bool:
    return bool(isinstance(value, str) and _TRACE_SHAPE.match(value))


def current() -> str:
    """The trace id of the work running now, or ``""`` outside any work."""
    return _CURRENT.get().trace


def context() -> Context:
    return _CURRENT.get()


def fields() -> dict[str, Any]:
    """Everything the log and the trace carry about the current work."""
    return _CURRENT.get().fields()


@contextmanager
def bind(
    trace_id: str,
    *,
    tenant: str = "",
    thread: str = "",
    turn: str = "",
    job: str = "",
    attempt: int = 0,
) -> Iterator[Context]:
    """Run a block as part of ``trace_id`` (nested binds add what they know)."""
    outer = _CURRENT.get()
    ctx = Context(
        trace=trace_id or outer.trace,
        tenant=tenant or outer.tenant,
        thread=thread or outer.thread,
        turn=turn or outer.turn,
        job=job or outer.job,
        attempt=attempt or outer.attempt,
    )
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)


@contextmanager
def bind_turn(
    turn_id: str, *, tenant: str, thread: str = "", attempt: int = 0,
    trace_id: str = "",
) -> Iterator[Context]:
    """Bind one turn: its trace id, distributor, thread and attempt number."""
    with bind(trace_id or turn_trace(turn_id), tenant=tenant, thread=thread,
              turn=turn_id, attempt=attempt) as ctx:
        yield ctx


@contextmanager
def bind_job(
    *, job_id: str, tenant: str, thread: str = "", trace_id: str = "",
    attempt: int = 0,
) -> Iterator[Context]:
    """Bind a runtime job. Its trace follows the turn that started it."""
    with bind(trace_id or job_id, tenant=tenant, thread=thread, job=job_id,
              attempt=attempt) as ctx:
        yield ctx


def stamp(payload: Mapping[str, Any], *, key: str = TOKEN) -> dict[str, Any]:
    """A copy of ``payload`` with the current trace id added (when there is one)."""
    out = dict(payload)
    trace = current()
    if trace and key not in out:
        out[key] = trace
    return out


def attempt(payload: Mapping[str, Any]) -> int:
    raw = payload.get(ATTEMPT)
    return int(raw) if isinstance(raw, int | str) and str(raw).isdigit() else 0


# ------------------------------------------------------------------- logs


class TraceFilter(logging.Filter):
    """Put the current trace, tenant, turn and attempt on every log record.

    Installed once with ``install_logging()``. A record made inside a bound
    block gets ``record.trace`` (and friends) even if the caller forgot to pass
    them, so ``%(trace)s`` works in every formatter and no log line is an
    orphan. A record that already carries its own trace keeps it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _CURRENT.get()
        for key, value in ctx.fields().items():
            if getattr(record, key, None) in (None, ""):
                setattr(record, key, value)
        if not getattr(record, "trace", ""):
            record.trace = "-"
        return True


_FILTER = TraceFilter()
_INSTALLED = False
_FORMAT = "%(asctime)s %(levelname)s trace=%(trace)s %(name)s: %(message)s"


def install_logging(*, format: str = _FORMAT, level: int = logging.INFO) -> None:
    """Make the root log line carry ``trace=``; safe to call more than once."""
    global _INSTALLED
    root = logging.getLogger()
    if not any(isinstance(f, TraceFilter) for f in root.filters):
        root.addFilter(_FILTER)
    if not _INSTALLED and not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(format))
        root.addHandler(handler)
        root.setLevel(level)
    _INSTALLED = True


def with_trace(record: logging.LogRecord, trace_id: str) -> logging.LogRecord:
    """Tag one record from an exporter or an error handler that has the id."""
    if not getattr(record, "trace", ""):
        record.trace = trace_id
    return record


def copy(ctx: Context, **changes: Any) -> Context:
    """A context with a few fields changed (for a nested hop)."""
    return replace(ctx, **changes)
