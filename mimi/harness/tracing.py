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
from dataclasses import dataclass, replace
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
                f"wake:{self.wake_source}"]


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
                # The model's plain text is private reasoning: it never
                # leaves in a trace (only whether there was any).
                metadata={"cost_kobo": ev.cost_kobo,
                          "private_text_chars": len(str(ev.data.get("private_text", "")))}))
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
        "cached_tokens": result.cached_tokens,
    })
    if result.route:
        # RAI-280: the route decision and its confidence on every trace.
        turn.metadata["route"] = result.route
        turn.score("route_confidence", float(result.route.get("confidence", 0.0)), "NUMERIC")
    if result.context:
        # RAI-281: the manifest and the section hashes on every trace.
        turn.metadata.update(result.context)
        turn.score("context_tokens", float(result.context.get("context_tokens", 0)), "NUMERIC")
        turn.score("manifest_present", True, "BOOLEAN")
    turn.observations.extend(run_observations(evs))
    turn.score("run_status", result.status, "CATEGORICAL")
    turn.score("stopped_budget", result.status == "stopped_budget", "BOOLEAN")
    turn.score("side_effect_in_loop", False, "BOOLEAN",
               "write tools return intents; Go applies them after the step is saved")
    turn.score("model_calls", float(result.model_calls), "NUMERIC")
    turn.score("intents", float(len(result.intents)), "NUMERIC")
    return turn


@dataclass
class RouteTurn(Turn):
    """A routing decision on its own (rules, phrases or the model)."""

    layer: str = ""
    skill: str = ""

    @property
    def name(self) -> str:  # type: ignore[override]
        return "route"

    @property
    def feature(self) -> str:  # type: ignore[override]
        return "router"

    @property
    def session_id(self) -> str:  # type: ignore[override]
        return self.thread_id

    @property
    def tags(self) -> list[str]:  # type: ignore[override]
        return ["router", f"layer:{self.layer}", f"skill:{self.skill}"]


def build_route_turn(decision: Any, *, synthetic: bool = True) -> RouteTurn:
    """``decision`` is a ``mimi.harness.router.RouteDecision``."""
    at = decision.events[0].at if decision.events else None
    start = int(at.timestamp() * 1e9) if at else 0
    end = start + int(decision.latency_ms * 1e6)
    t = RouteTurn(job_id=f"route:{decision.external_id}", flow="receipt_reply",
                  shop_id="router", thread_id=decision.thread_id, started_ns=start,
                  ended_ns=end, synthetic=synthetic, layer=decision.layer,
                  skill=decision.skill)
    t.verdict = decision.status
    t.language = decision.language
    t.input = {"source": decision.source, "external_id": decision.external_id}
    t.output = {"status": decision.status, "helper": decision.helper, "skill": decision.skill,
                "clarify_question": decision.clarify_question,
                "options": list(decision.options)}
    t.metadata.update({
        "route": {"helper": decision.helper, "skill": decision.skill,
                  "confidence": decision.confidence, "layer": decision.layer,
                  "rule": decision.rule, "lane": decision.lane,
                  "secondary": list(decision.secondary),
                  "injection": list(decision.injection),
                  "downgraded_from": decision.downgraded_from},
        "thresholds": decision.thresholds, "catalogue_version": decision.catalogue_version,
        "prompt_version": decision.prompt_version, "latency_ms": decision.latency_ms,
        "local_ms": decision.local_ms, "model_ms": decision.model_ms})
    if decision.model_called:
        t.observations.append(Observation(
            "generation", f"model {decision.model}", start,
            duration_ns=int(decision.model_ms * 1e6), model=decision.model,
            usage={"input": decision.prompt_tokens, "output": decision.completion_tokens},
            output={"skill": decision.skill}, metadata={"cost_kobo": decision.cost_kobo}))
    t.score("route_confidence", float(decision.confidence), "NUMERIC")
    t.score("route_layer", decision.layer, "CATEGORICAL")
    t.score("route_clarified", decision.status == "clarify", "BOOLEAN")
    t.score("injection_seen", bool(decision.injection), "BOOLEAN")
    return t


def trace_route(decision: Any, exporter: TurnExporter, **kw: Any) -> RouteTurn | None:
    """Export one routing decision (no-op when the exporter is off). Never raises."""
    if not getattr(exporter, "enabled", False):
        return None
    try:
        t = build_route_turn(decision, **kw)
        exporter.export(t)
        return t
    except Exception:
        return None


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


@dataclass
class JobTurn(HarnessTurn):
    """One durable job (a turn of the API's queue): its route, its run, its
    state changes across worker crashes, parks and resumes, and its spend."""

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"job-{self.helper or 'mimi'}"

    @property
    def feature(self) -> str:  # type: ignore[override]
        return "job"

    @property
    def tags(self) -> list[str]:  # type: ignore[override]
        return ["job", f"helper:{self.helper}", f"status:{self.status}",
                f"wake:{self.wake_source}"]


def build_job_turn(decision: Any, result: RunResult | None, *, job_id: str, status: str,
                   job: dict[str, Any], shop_id: str = "owner",
                   synthetic: bool = True) -> JobTurn:
    """The job's one trace: the route as its first span, then the run's steps."""
    route = build_route_turn(decision, synthetic=synthetic)
    base = build_turn(result, shop_id=shop_id, synthetic=synthetic) if result else None
    turn = JobTurn(job_id=job_id, flow="receipt_reply", shop_id=shop_id,
                   thread_id=decision.thread_id, started_ns=route.started_ns,
                   ended_ns=max(route.ended_ns, base.ended_ns if base else 0),
                   synthetic=synthetic, helper=decision.helper, status=status,
                   wake_source=str(decision.source))
    turn.verdict = status
    turn.input = {"source": decision.source, "external_id": decision.external_id}
    turn.output = base.output if base else {"status": status, "skill": decision.skill}
    if base:
        turn.metadata.update(base.metadata)
    route_obs = Observation("span", "route", route.started_ns,
                            duration_ns=max(1, route.ended_ns - route.started_ns),
                            output=route.output, metadata=dict(route.metadata))
    route_obs.children.extend(route.observations)
    turn.observations.append(route_obs)
    if base:
        turn.observations.extend(base.observations)
    run_kobo = int(result.spend.kobo) if result else 0
    route_kobo = int(getattr(decision, "cost_kobo", 0) or 0)
    turn.metadata["job"] = {**job, "status": status, "spend_kobo": run_kobo + route_kobo,
                            "route_kobo": route_kobo, "run_kobo": run_kobo}
    if base:
        turn.scores.extend(replace(f, trace_seed=turn.trace_seed) for f in base.scores)
    turn.score("job_attempts", float(job.get("attempts", 1)), "NUMERIC")
    turn.score("job_resumed", bool(job.get("resumed")), "BOOLEAN")
    turn.score("job_spend_kobo", float(run_kobo + route_kobo), "NUMERIC")
    return turn


def trace_job(decision: Any, result: RunResult | None, exporter: TurnExporter, *,
              job_id: str, status: str, job: dict[str, Any], **kw: Any) -> JobTurn | None:
    """Export one job as one trace (no-op when the exporter is off). Never raises."""
    if not getattr(exporter, "enabled", False):
        return None
    try:
        t = build_job_turn(decision, result, job_id=job_id, status=status, job=job, **kw)
        exporter.export(t)
        return t
    except Exception:
        return None
