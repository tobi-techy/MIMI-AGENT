"""The harness as a durable job step (RAI-297 slice): ``step(job) -> StepResult``.

``harness_steps(harness, helpers)`` gives the ``JobRunner`` one step function
per helper. Each call:

1. loads the loop state saved with the job (``Job.scratch["harness"]``), or
   starts a new one from the job's goal, wake source, plan, to-do and note;
2. runs exactly one ``Harness.step`` (kill switch and daily cap first; the
   runner already checked the job's step, time and naira budgets and took
   owner messages at the boundary, which steer this step);
3. returns the step's intents as runtime intents, the updated plan, to-do
   and note, the step's naira cost and the new loop state, all of which the
   runner saves in one compare-and-set before the outbox hands anything to Go.

Nothing lives in the worker between steps: kill every worker, start a new
one, and it carries on from the saved state. A step that crashed before its
save is run again from the same saved state; its intents carry the same
business keys, so Go applies them once.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from mimi.harness.loop import Harness, Helper, LoopState
from mimi.harness.types import IntentRecord, OutboundDraft, RunEvent, WakeSource
from mimi.runtime.outbox import intent_types
from mimi.runtime.steps import StepContext, StepFn, StepOutcome
from mimi.types import ShopScope


@dataclass(frozen=True)
class HarnessIntent:
    """The runtime's envelope for a harness intent. Go reads ``record``.

    ``record`` is the ``IntentRecord`` as JSON: tool, intent kind, Go-policy
    tier, scope, business idempotency key, attempt key, payload, preview.
    """

    scope: ShopScope
    intent_kind: str
    record: dict[str, Any] = field(default_factory=dict)
    kind: Literal["harness_intent"] = field(default="harness_intent")


HARNESS_INTENTS: tuple[type, ...] = intent_types(HarnessIntent)


def _scope(tenant_id: str, shop_id: str) -> ShopScope:
    return ShopScope(tenant_id=tenant_id, shop_id=shop_id or "org")


def to_runtime(rec: IntentRecord) -> HarnessIntent:
    return HarnessIntent(_scope(rec.tenant_id, rec.shop_id), rec.intent_kind,
                         rec.model_dump(mode="json"))


def draft_intent(d: OutboundDraft, job_id: str, *, shadow: bool = False) -> HarnessIntent:
    """An owner question the harness wrote itself (budget, daily cap)."""
    rec = {"tool": "send_to_user", "intent_kind": "send_to_user", "tier": d.tier,
           "scope": "org:owner", "tenant_id": d.tenant_id, "shop_id": d.shop_id,
           "idempotency_key": d.idempotency_key, "attempt_key": d.idempotency_key,
           "payload": {"channel": d.channel, "body": d.body, "kind": d.kind},
           "preview": d.body, "step": d.step, "shadow": shadow}
    return HarnessIntent(_scope(d.tenant_id, d.shop_id), "send_to_user", rec)


_WAKES = set(get_args(WakeSource))


def harness_steps(harness: Harness, helpers: Mapping[str, Helper]) -> dict[str, StepFn]:
    def make(helper: Helper) -> StepFn:
        def step(ctx: StepContext) -> StepOutcome:
            raw = ctx.scratch.get("harness")
            if raw:
                state = LoopState.model_validate(raw)
                if state.finished is not None:
                    # The job parked (a question, an approval, a budget, a
                    # timer) and something woke it: carry on the same run.
                    status = state.finished.status
                    state = harness.reopen(state)
                    if not ctx.new_owner_messages:
                        state.messages.append({"role": "user", "content": (
                            f"[Mimi harness] Woke again ({ctx.wake_source}) after "
                            f"{status}. Check what changed before acting.")})
            else:
                wake = ctx.wake_source if ctx.wake_source in _WAKES else "routine"
                state = harness.start(
                    RunEvent(event_id=f"job:{ctx.job_id}", tenant_id=ctx.tenant_id,
                             thread_id=ctx.thread_id or ctx.job_id,
                             wake_source=wake,  # type: ignore[arg-type]
                             text=ctx.goal, sender="owner", at=ctx.now, job_id=ctx.job_id),
                    helper, plan=ctx.plan, todo=ctx.todo, note=ctx.working_note)
            res = harness.step(state, helper, owner_messages=ctx.new_owner_messages,
                               enforce_run_budget=False)
            keys = {i.idempotency_key for i in res.new_intents}
            intents: list[HarnessIntent] = [to_runtime(i) for i in res.new_intents]
            intents += [draft_intent(d, ctx.job_id, shadow=helper.shadow) for d in res.new_drafts
                        if d.idempotency_key not in keys]
            trace = _trace(res.new_events, ctx)
            common: dict[str, Any] = dict(
                intents=tuple(intents), cost_kobo=res.cost_kobo, plan=res.plan,
                todo=res.todo, working_note=res.working_note,
                scratch={"harness": res.state}, trace=trace,
            )
            result = res.result
            if result is None:
                return StepOutcome(kind="continue", **common)
            if result.status in ("done", "done_quiet"):
                return StepOutcome(kind="done", result=f"{result.status}: {result.reason}",
                                   **common)
            if result.status == "parked_for_timer" and result.follow_up_at is not None:
                return StepOutcome(kind="sleep", sleep_until=result.follow_up_at, **common)
            if result.status in ("parked_for_approval", "asked_question", "stopped_budget"):
                return StepOutcome(kind="ask_owner",
                                   question=result.owner_question or result.reason, **common)
            return StepOutcome(kind="blocked", question=f"{result.status}: {result.reason}",
                               **common)

        return step

    return {name: make(h) for name, h in helpers.items()}


def _trace(events: Any, ctx: StepContext) -> dict[str, str | int | float]:
    """Step attributes for the runtime span: tool, short result, duration, naira."""
    out: dict[str, str | int | float] = {"harness": 1, "wake_source": ctx.wake_source}
    for ev in events:
        if ev.type == "step_finished":
            out.update({"tool": ev.tool, "short": ev.short[:200],
                        "duration_ms": round(ev.duration_ms, 3), "cost_kobo": ev.cost_kobo,
                        "model": ev.model})
        elif ev.type == "run_finished":
            out["run_status"] = str(ev.data.get("status", ""))
    return out


def replay(outbox: list[HarnessIntent]) -> list[dict[str, Any]]:
    """Plain dicts of runtime intents (tests and the eval read them)."""
    return [dataclasses.asdict(i) for i in outbox]

