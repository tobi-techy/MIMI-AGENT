"""One Langfuse trace per harness run, with a span per step.

Built on the exporter from ``mimi.observe`` (off without keys and always in
CI; redaction before anything leaves). ``trace_run(result, exporter)`` turns
a finished ``RunResult``'s events into the observation tree Langfuse shows:

    run-collector (agent)              trace: session = thread, tags, wake source
      step-1 (chain)                   tool names, short result, naira, duration
        model muse-spark-1.3 (generation)   tokens, cost, fallback if any
        get_invoices (tool)            tier, scope, latency, short result
        propose_reminder_plan (tool)   tier approve, idempotency key
      step-2 ...

Scores: ``run_status`` (categorical), ``stopped_budget`` and
``side_effect_in_loop`` (boolean; the second must always be false),
``model_calls`` and ``intents`` (numeric). Metadata carries the wake source,
the toolset hash, the registry and skill versions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from mimi.harness.types import Event, RunResult
from mimi.observe.turns import Observation, Turn, TurnExporter


@dataclass
class HarnessTurn(Turn):
    """A turn that is a whole helper run, not one shop message."""

    helper: str = ""
    status: str = ""
    wake_source: str = ""

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"run-{self.helper}"

    @property
    def feature(self) -> str:  # type: ignore[override]
        return "harness"

    @property
    def session_id(self) -> str:  # type: ignore[override]
        return self.thread_id

    @property
    def tags(self) -> list[str]:  # type: ignore[override]
        return ["harness", f"helper:{self.helper}", f"status:{self.status}",
                f"wake:{self.wake_source}", "distributor:coral-lane"]


def _ns(ev: Event) -> int:
    return int(ev.at.timestamp() * 1e9)


def run_observations(events: Sequence[Event]) -> list[Observation]:
    steps: list[Observation] = []
    current: Observation | None = None
    for ev in events:
        if ev.type == "step_started":
            current = Observation("chain", f"step-{ev.step + 1}", _ns(ev),
                                  metadata={"step": ev.step + 1})
            steps.append(current)
            continue
        if current is None:
            continue
        if ev.type == "model_called":
            current.children.append(Observation(
                "generation", f"model {ev.model}", _ns(ev), duration_ns=int(ev.duration_ms * 1e6),
                model=ev.model,
                usage={"input": int(ev.data.get("prompt_tokens", 0)),
                       "output": int(ev.data.get("completion_tokens", 0))},
                output={"tool_calls": ev.short},
                metadata={"cost_kobo": ev.cost_kobo,
                          "private_text": str(ev.data.get("private_text", ""))[:200]}))
        elif ev.type == "model_fallback":
            current.children.append(Observation("span", "model-fallback", _ns(ev),
                                                level="WARNING", status_message=ev.short))
        elif ev.type in ("tool_result", "intent_proposed", "tool_refused"):
            current.children.append(Observation(
                "tool", ev.tool or ev.type, _ns(ev), duration_ns=int(ev.duration_ms * 1e6),
                output={"short": ev.short},
                metadata={"tier": ev.tier, "scope": ev.scope, "event": ev.type,
                          **({"idempotency_key": ev.data["idempotency_key"]}
                             if "idempotency_key" in ev.data else {})},
                level="WARNING" if ev.type == "tool_refused" else "DEFAULT"))
        elif ev.type in ("repair_requested", "model_output_invalid", "field_ignored"):
            current.children.append(Observation("span", ev.type, _ns(ev), level="WARNING",
                                                status_message=ev.short))
        elif ev.type == "step_finished":
            current.duration_ns = int(ev.duration_ms * 1e6)
            current.output = {"tools": ev.tool, "short": ev.short}
            current.metadata.update({"cost_kobo": ev.cost_kobo,
                                     "duration_ms": round(ev.duration_ms, 3)})
    for ev in events:
        if ev.type in ("budget_hit", "cap_hit", "kill_switch"):
            steps.append(Observation("span", ev.type, _ns(ev), level="WARNING",
                                     status_message=ev.short))
    return steps


def build_turn(result: RunResult, *, shop_id: str = "owner", synthetic: bool = True,
               goal: str = "") -> HarnessTurn:
    evs = result.events
    start = _ns(evs[0]) if evs else 0
    end = _ns(evs[-1]) if evs else 0
    turn = HarnessTurn(
        job_id=result.job_id, flow="receipt_reply", shop_id=shop_id,
        thread_id=result.thread_id, started_ns=start, ended_ns=end, synthetic=synthetic,
        helper=result.helper, status=result.status, wake_source=result.wake_source,
    )
    turn.verdict = result.status
    turn.input = {"goal": goal, "wake_source": result.wake_source}
    turn.output = {"status": result.status, "reason": result.reason,
                   "intents": [i.preview for i in result.intents],
                   "drafts": [d.body for d in result.outbound_drafts]}
    turn.metadata.update({
        "helper": result.helper, "wake_source": result.wake_source,
        "toolset_hash": result.toolset_hash, "registry_version": result.registry_version,
        "run_id": result.run_id, "steps": result.spend.steps,
        "spend_kobo": result.spend.kobo, "loop_overhead_ms": result.loop_overhead_ms,
    })
    turn.observations.extend(run_observations(evs))
    turn.score("run_status", result.status, "CATEGORICAL")
    turn.score("stopped_budget", result.status == "stopped_budget", "BOOLEAN")
    turn.score("side_effect_in_loop", False, "BOOLEAN",
               "write tools return intents; Go applies them after the step is saved")
    turn.score("model_calls", float(result.model_calls), "NUMERIC")
    turn.score("intents", float(len(result.intents)), "NUMERIC")
    return turn


def trace_run(result: RunResult, exporter: TurnExporter, **kw: Any) -> HarnessTurn | None:
    """Export one run (no-op when the exporter is off). Never raises."""
    if not getattr(exporter, "enabled", False):
        return None
    try:
        turn = build_turn(result, **kw)
        exporter.export(turn)
        return turn
    except Exception:  # tracing never breaks a run
        return None
