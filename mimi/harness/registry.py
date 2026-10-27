"""Typed tool registry: every tool declares what it reads, what it may propose,
the scope it needs, its idempotency rule and how to dry-run it.

A tool is a plain function ``fn(ctx, args) -> output`` with Pydantic models
for ``args`` and ``output``, registered with ``@registry.tool(...)``. The
registry is shared across helpers; each helper gets a fixed ``Toolset`` from
its allow list, built once at startup:

* a tool outside the allow list is refused at call time and logged;
* a tool whose Go-policy tier is ``never`` (moving money, changing bank
  details) cannot be put in any helper's toolset: building it raises;
* a tool whose name carries a money verb cannot be put in a toolset either;
* every write tool must have a ``dry_run`` (the verifier and shadow mode use
  it), or registration raises;
* each tool's description plus JSON schema must stay under 4,000 characters
  (``MAX_TOOL_CHARS``), checked when the toolset is built and in CI;
* the toolset's hash is fixed until a deploy changes the registry, so the
  prompt cache prefix stays byte-identical across every turn of a thread.

The tier a tool declares here is documentation and a default for tests; the
tier that counts is read from Go policy (``mimi.harness.policy``) for every
call, and a tier the model names in its arguments is ignored.
"""

from __future__ import annotations

import hashlib
import json
import typing
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from mimi.harness.policy import RiskPolicy
from mimi.harness.types import COST_ORDER, CostTier, Tier, ToolKind
from mimi.runtime.outbox import MONEY_VERBS

MAX_TOOL_CHARS = 4_000
# Argument names the model may not use to steer the harness. They are
# dropped before validation and logged (``field_ignored``).
RESERVED_ARGS: frozenset[str] = frozenset(
    {"tier", "risk_tier", "risk", "approved", "approval", "scope", "idempotency_key",
     "dry_run", "tenant_id", "skip_approval"}
)


class RegistryError(ValueError):
    """A tool or toolset that must not exist. Raised at startup, never at run time."""


@dataclass(frozen=True)
class ToolContext:
    """What a tool may see: the run's tenant, its consent grants and Go's read API.

    Tools never get the store, the outbox or a database handle, so a write
    tool can only describe an intent; it cannot perform one.
    """

    run_id: str
    job_id: str
    thread_id: str
    tenant_id: str
    helper: str
    step: int
    now: datetime
    book: Any  # mimi.harness.go.BookAPI (Any keeps tools importable without Go)
    connectors: Any  # mimi.harness.go.Connectors
    grants: Any  # mimi.harness.go.ConsentGrants for this run


ToolFn = Callable[[ToolContext, Any], Any]
DryRunFn = Callable[[ToolContext, Any, Any], str]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    kind: ToolKind
    scope: str
    tier: Tier
    idem: tuple[str, ...]
    fn: ToolFn
    dry_run: DryRunFn | None
    cost_tier: CostTier
    # The Go connector this tool needs ("bank_feed"); down means the tool
    # returns "connector down" and the helper asks the owner.
    connector: str = ""
    # The intent kind Go receives (defaults to the tool name).
    intent_kind: str = ""
    # One line for prompts that carry names and summaries only.
    summary: str = ""
    # A scope that depends on the arguments (send_to_user to a shop needs
    # shop:messages, to the owner org:owner). Defaults to ``scope``.
    scope_fn: Callable[[Any], str] | None = None

    def scope_for(self, args: Any) -> str:
        return self.scope_fn(args) if self.scope_fn else self.scope

    def schema(self, *, full: bool = True) -> dict[str, Any]:
        """The tool in OpenAI-style function-calling form."""
        params = _clean_schema(self.input_model.model_json_schema()) if full else {
            "type": "object", "additionalProperties": True}
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description if full else (self.summary or self.description),
                "parameters": params,
            },
        }

    def size(self) -> int:
        return len(json.dumps(self.schema(), separators=(",", ":")))


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Drop Pydantic's titles (noise in the prompt) and close every object."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: walk(v) for k, v in node.items() if k != "title"}
            if out.get("type") == "object" and "additionalProperties" not in out:
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    cleaned = walk(schema)
    assert isinstance(cleaned, dict)
    return cleaned


def has_money_verb(name: str) -> bool:
    low = name.lower()
    return any(verb in low for verb in MONEY_VERBS)


class ToolRegistry:
    """All tools, shared across helpers. ``version`` changes only with a deploy."""

    def __init__(self, version: str) -> None:
        self.version = version
        self._tools: dict[str, ToolSpec] = {}

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def get(self, name: str) -> ToolSpec:
        return self._tools[name]

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise RegistryError(f"tool {spec.name!r} is already registered")
        if not spec.description.strip():
            raise RegistryError(f"tool {spec.name!r} has no description")
        if spec.cost_tier not in COST_ORDER:
            raise RegistryError(f"tool {spec.name!r} has an unknown cost tier")
        if spec.kind == "write_intent":
            if spec.dry_run is None:
                raise RegistryError(f"write tool {spec.name!r} needs a dry_run")
            if not spec.idem:
                raise RegistryError(f"write tool {spec.name!r} needs idempotency fields")
            fields = set(spec.input_model.model_fields) | set(spec.output_model.model_fields)
            missing = [f for f in spec.idem if f not in fields]
            if missing:
                raise RegistryError(
                    f"tool {spec.name!r}: idempotency fields {missing} are not in its models"
                )
        self._tools[spec.name] = spec
        return spec

    def tool(
        self,
        *,
        kind: ToolKind,
        scope: str,
        tier: Tier,
        cost_tier: CostTier = "go_api",
        idem: Sequence[str] = (),
        dry_run: DryRunFn | None = None,
        connector: str = "",
        intent_kind: str = "",
        summary: str = "",
        name: str = "",
        scope_fn: Callable[[Any], str] | None = None,
    ) -> Callable[[ToolFn], ToolFn]:
        """Decorator. Input and output models come from the function's hints."""

        def wrap(fn: ToolFn) -> ToolFn:
            hints = typing.get_type_hints(fn)
            params = [p for p in fn.__code__.co_varnames[: fn.__code__.co_argcount]]
            if len(params) != 2:
                raise RegistryError(f"{fn.__name__}: a tool takes (ctx, args)")
            input_model = hints.get(params[1])
            output_model = hints.get("return")
            for label, model in (("args", input_model), ("return", output_model)):
                if not (isinstance(model, type) and issubclass(model, BaseModel)):
                    raise RegistryError(f"{fn.__name__}: {label} must be a Pydantic model")
            assert isinstance(input_model, type) and isinstance(output_model, type)
            doc = (fn.__doc__ or "").strip()
            description = " ".join(doc.split())
            self.register(
                ToolSpec(
                    name=name or fn.__name__,
                    description=description,
                    input_model=input_model,
                    output_model=output_model,
                    kind=kind,
                    scope=scope,
                    tier=tier,
                    idem=tuple(idem),
                    fn=fn,
                    dry_run=dry_run,
                    cost_tier=cost_tier,
                    connector=connector,
                    intent_kind=intent_kind or (name or fn.__name__),
                    summary=summary or description.split(". ")[0][:120],
                    scope_fn=scope_fn,
                )
            )
            return fn

        return wrap


@dataclass(frozen=True)
class Toolset:
    """One helper's fixed tools, checked once at startup."""

    helper: str
    registry_version: str
    specs: tuple[ToolSpec, ...]
    full_schemas: bool = True
    _by_name: dict[str, ToolSpec] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        self._by_name.update({s.name: s for s in self.specs})

    def allowed(self, name: str) -> bool:
        return name in self._by_name

    def get(self, name: str) -> ToolSpec:
        return self._by_name[name]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.specs)

    def schemas(self) -> list[dict[str, Any]]:
        return [s.schema(full=self.full_schemas) for s in self.specs]

    @property
    def hash(self) -> str:
        """Stable across turns: the same registry version gives the same bytes."""
        canon = json.dumps(
            {"v": self.registry_version, "helper": self.helper, "tools": self.schemas()},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canon.encode()).hexdigest()[:16]


def build_toolset(
    registry: ToolRegistry,
    helper: str,
    allow: Iterable[str],
    policy: RiskPolicy,
    *,
    full_schemas: bool = True,
) -> Toolset:
    """The startup check. Raises ``RegistryError`` on anything unsafe."""
    specs: list[ToolSpec] = []
    seen: set[str] = set()
    for name in allow:
        if name in seen:
            raise RegistryError(f"{helper}: {name!r} listed twice")
        seen.add(name)
        if name not in registry:
            raise RegistryError(f"{helper}: unknown tool {name!r}")
        spec = registry.get(name)
        if has_money_verb(name) or has_money_verb(spec.intent_kind):
            raise RegistryError(f"{helper}: {name!r} names a money verb; Mimi never moves money")
        if policy.base_tier(name) == "never" or spec.tier == "never":
            raise RegistryError(f"{helper}: {name!r} is tier never in Go policy")
        if spec.size() > MAX_TOOL_CHARS:
            raise RegistryError(
                f"{helper}: {name!r} description plus schema is {spec.size()} chars "
                f"(max {MAX_TOOL_CHARS})"
            )
        specs.append(spec)
    return Toolset(helper, registry.version, tuple(specs), full_schemas)
