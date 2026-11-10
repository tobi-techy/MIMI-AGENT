"""The one way trace data leaves the process, and the guard that makes it true.

``mimi.observe.redact`` says what a trace may contain. This module makes every
sink ask it, and stops a leak before it is sent rather than after:

* ``export(exporter, turn)`` and ``feedback(exporter, fb)`` are the **only**
  functions that call a ``TurnExporter``. Each builds the payload the exporter
  would send, redacts it once with ``redact``, then checks the redacted copy
  with ``redact.find_leaks``. A payload that still looks like a phone number,
  a long digit run or a key is **dropped**: ``LeakError`` in CI and in tests
  (so the PR cannot land), a counted drop in production (a trace is a nice to
  have, a shop's number is not).
* The exporter itself is handed the **redacted** turn, not the raw one. So an
  exporter written next month that forgets to redact still cannot send a
  phone number or model text: it never sees one.
* ``log_event(name, fields)`` is the one structured log line. It redacts and
  checks the same way, so an agent adding ``log.info("...")`` with a phone in
  it fails the test that drives every sink with the same hostile payload.

``tests/test_observe_sinks.py`` fails when a sink is bypassed: a module scan
refuses a direct ``.export(``/``.feedback(`` call outside this file, and the
guard is driven with a hostile turn and a deliberately broken redactor.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from mimi.observe import redact as R

if TYPE_CHECKING:  # avoids a cycle: ``turns`` uses ``sinks`` to export
    from mimi.observe.turns import Feedback, Observation, Turn, TurnExporter

log = logging.getLogger(__name__)

# A payload that would still leak, after redaction. Only the field paths and
# the kind of data are carried, never the data itself.
_LEAK_FIELDS_CAP = 8


class LeakError(RuntimeError):
    """A trace or log line would still carry private data after redaction."""

    def __init__(self, where: str, leaks: list[str]) -> None:
        self.where = where
        self.leaks = leaks[:_LEAK_FIELDS_CAP]
        super().__init__(f"{where} would leak private data in: {', '.join(self.leaks)}")


def strict() -> bool:
    """Raise on a leak (CI, tests) instead of dropping the payload."""
    raw = os.environ.get("MIMI_TRACE_STRICT", "").strip()
    if raw and raw != "0":
        return True
    return os.environ.get("CI", "").strip().lower() in ("1", "true")


DROPPED: list[dict[str, Any]] = []


def _drop(where: str, leaks: list[str]) -> None:
    DROPPED.append({"where": where, "leaks": leaks[:_LEAK_FIELDS_CAP]})
    if len(DROPPED) > 1000:
        del DROPPED[:500]


# --------------------------------------------------------------- payloads


def payload_of(turn: Turn) -> dict[str, Any]:
    """Everything an exporter may send for this turn, in one structure."""
    return {
        "job_id": turn.job_id,
        "flow": turn.flow,
        "shop_id": turn.shop_id,
        "thread_id": turn.thread_id,
        "verdict": turn.verdict,
        "language": turn.language,
        "number": turn.number,
        "name": turn.name,
        "tags": turn.tags,
        "user_id": turn.user_id,
        "session_id": turn.session_id,
        "input": turn.input,
        "output": turn.output,
        "metadata": turn.metadata,
        "observations": [_observation_payload(o) for o in turn.observations],
        "scores": [{"name": s.name, "value": s.value, "comment": s.comment}
                   for s in turn.scores],
        "synthetic": turn.synthetic,
    }


def _observation_payload(obs: Observation) -> dict[str, Any]:
    return {
        "kind": obs.kind,
        "name": obs.name,
        "model": obs.model,
        "usage": obs.usage,
        "input": obs.input,
        "output": obs.output,
        "metadata": obs.metadata,
        "status_message": obs.status_message,
        "children": [_observation_payload(c) for c in obs.children],
    }


def _observation_redacted(obs: Observation) -> Observation:
    return replace(
        obs,
        input=R.redact(obs.input),
        output=R.redact(obs.output),
        metadata=R.redact(obs.metadata),
        status_message=R.redact(obs.status_message),
        children=[_observation_redacted(c) for c in obs.children],
        # Media payloads are attached by the exporter itself, from the sha256
        # its own allowlist chose; the bytes never ride on the turn's fields.
    )


def redacted_turn(turn: Turn) -> Turn:
    """The turn an exporter is allowed to see: every free-text field redacted."""
    return replace(
        turn,
        input=R.redact(turn.input),
        output=R.redact(turn.output),
        metadata=R.redact(turn.metadata),
        observations=[_observation_redacted(o) for o in turn.observations],
        scores=[replace(s, comment=R.redact(s.comment)) for s in turn.scores],
    )


def clean(where: str, payload: Any) -> Any:
    """Redact ``payload`` and refuse it if anything private survived."""
    safe = R.redact(payload)
    leaks = R.find_leaks(safe)
    if leaks:
        _drop(where, leaks)
        if strict():
            raise LeakError(where, leaks)
        log.error("trace: %s dropped, %d field(s) would leak: %s", where, len(leaks),
                  ", ".join(leaks[:_LEAK_FIELDS_CAP]))
        return None
    return safe


# ------------------------------------------------------------------ sinks


def export(exporter: TurnExporter, turn: Turn) -> None:
    """Send one turn to ``exporter``, redacted and checked first."""
    if not getattr(exporter, "enabled", False):
        return
    payload = payload_of(turn)
    if clean("trace", payload) is None:
        return
    exporter.export(redacted_turn(turn))


def feedback(exporter: TurnExporter, fb: Feedback) -> None:
    """Send one score, with its comment redacted and checked first."""
    if not getattr(exporter, "enabled", False):
        return
    if clean("score", {"name": fb.name, "comment": fb.comment, "value": fb.value}) is None:
        return
    exporter.feedback(replace(fb, comment=R.redact_text(fb.comment)))


def log_event(name: str, fields: Mapping[str, Any] | None = None, *,
              level: int = logging.INFO) -> None:
    """One structured log line: redacted, checked, and tagged with the trace.

    The trace id and the attempt come from ``mimi.observe.correlation`` when a
    block is bound; the caller never builds them by hand, so every line of one
    piece of work matches.
    """
    from mimi.observe import correlation

    body = dict(fields or {})
    body.update({k: v for k, v in correlation.fields().items() if k not in body})
    safe = clean("log", body)
    if safe is None:
        return
    log.log(level, "%s %s", name, json.dumps(safe, default=str, sort_keys=True))
