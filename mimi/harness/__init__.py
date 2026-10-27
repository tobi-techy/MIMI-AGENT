"""The agent harness: one loop every helper runs on, and the typed tools it calls.

Built during the Meta Global AI Developer Hackathon (RAI-278, RAI-279).
In-house and framework-free: a few hundred lines of Python we fully own,
model-agnostic through OpenAI-style tool calling.

The one rule: **Mimi proposes, code checks, the owner approves.**

* ``loop``: ``Harness.run(event, helper) -> RunResult`` and
  ``Harness.step(state, helper) -> StepResult`` with step, time and naira
  budgets, a daily naira cap and kill switches, one repair then fail safe,
  model fallback, ordered events.
* ``registry``: ``@registry.tool`` with typed inputs and outputs, scope, risk
  tier (read from Go policy, never the model), idempotency fields, dry run,
  cost tier; per-helper fixed toolsets with a startup check.
* ``collector``: the first hire's 13 tools, its skill and helper config.
* ``durable``: the loop as a step of the always-on job runner.
* ``go``: the system-of-record ports and ``FakeGo`` over the synthetic world.
* ``gateway``: tool-calling model client, fallback chain, cost in kobo,
  scripted and record/replay doubles.
* ``tracing``: one Langfuse trace per run, a span per step.

Mimi never moves money: no tool can, a ``never``-tier tool cannot be put in
any toolset, and write tools only return intents that Go validates.
"""

from mimi.harness.collector import collector_helper, default_registry, proof_helper
from mimi.harness.gateway import Gateway, ModelTurn, ProviderError, ScriptedModel, say
from mimi.harness.go import ConsentGrants, FakeGo
from mimi.harness.loop import Harness, Helper, LoopState, RunBudget
from mimi.harness.policy import GoPolicy
from mimi.harness.registry import RegistryError, ToolRegistry, Toolset, build_toolset
from mimi.harness.types import Event, IntentRecord, OutboundDraft, RunEvent, RunResult

__all__ = [
    "ConsentGrants",
    "Event",
    "FakeGo",
    "Gateway",
    "GoPolicy",
    "Harness",
    "Helper",
    "IntentRecord",
    "LoopState",
    "ModelTurn",
    "OutboundDraft",
    "ProviderError",
    "RegistryError",
    "RunBudget",
    "RunEvent",
    "RunResult",
    "ScriptedModel",
    "ToolRegistry",
    "Toolset",
    "build_toolset",
    "collector_helper",
    "default_registry",
    "proof_helper",
    "say",
]
