"""Model gateway: OpenAI-style tool calling, a fallback chain, cost in kobo.

The harness is model-agnostic. Every model it calls speaks OpenAI-style
Chat Completions with ``tools`` (Meta's Model API does: Muse Spark returns
``tool_calls``), so swapping Muse Spark for another provider behind the
gateway (llmapi.ai or Cencori, RAI-226) is configuration.

* ``OpenAIToolModel``: plain httpx, one POST, a timeout. It does not retry:
  a timeout, a connection error, 429 or 5xx raises ``ProviderError`` and the
  ``Gateway`` moves to the next model in the chain within the same run.
* ``Gateway``: the chain (primary first), per-call timeout, and the kobo
  cost of each call from token usage (``kobo_cost``).
* ``ScriptedModel``: the deterministic test double. Its script sees the whole
  transcript and the tool schemas and returns the next turn (or raises), so
  tests and the scripted eval run offline and repeatably.
* ``RecordedToolModel``: record/replay for live evals. Live calls are
  recorded by request hash; CI replays them and never calls out.

The key is read by the caller and passed in; it is never logged.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from mimi.models import metering
from mimi.models.cache import note_migration, requests_index, versioned_key

# Prices and the naira rate live in one place (``mimi.models.prices``, with
# their source); re-exported here for the callers that import them from the
# gateway.
from mimi.models.prices import DEFAULT_USD_NGN as DEFAULT_USD_NGN
from mimi.models.prices import kobo_cost as kobo_cost
from mimi.models.prices import usd_to_kobo_rate as usd_to_kobo_rate


def worst_case_kobo(model: str, prompt_chars: int, max_tokens: int) -> int:
    """The most the next call can cost: prompt (about 3 chars a token) plus the cap."""
    return kobo_cost(model, prompt_chars // 3 + 1, max_tokens)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text, exactly as the model wrote it


@dataclass(frozen=True)
class ModelTurn:
    content: str
    tool_calls: tuple[ToolCall, ...]
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    fake: bool = False
    # A scripted turn may name its own cost (budget tests); else priced.
    cost_kobo: int | None = None
    # "length" means the completion cap cut the answer off (reasoning models
    # can spend it all thinking); the loop treats that as invalid output.
    finish_reason: str = ""
    # Prompt tokens the provider served from its prompt cache.
    cached_tokens: int = 0


class ProviderError(RuntimeError):
    """The provider failed (timeout, connection, 429, 5xx). Try the next model."""

    def __init__(self, model: str, detail: str) -> None:
        self.model = model
        self.detail = detail
        super().__init__(f"{model}: {detail}")


class ToolModel(Protocol):
    name: str

    def chat(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]], *,
        max_tokens: int,
    ) -> ModelTurn: ...


_RETRYABLE = frozenset({408, 409, 429, 500, 502, 503, 504})


class OpenAIToolModel:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.meta.ai/v1",
        timeout_s: float = 30.0,
        reasoning_effort: str | None = "low",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAIToolModel needs an API key")
        self.name = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._effort = reasoning_effort
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    def chat(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]], *,
        max_tokens: int,
    ) -> ModelTurn:
        metering.guard(self.name)
        body: dict[str, Any] = {
            "model": self.name,
            "messages": list(messages),
            "max_completion_tokens": max_tokens,
        }
        if tools:
            body["tools"] = list(tools)
            body["tool_choice"] = "auto"
        if self._effort:
            body["reasoning_effort"] = self._effort
        started = time.monotonic()
        try:
            resp = self._client.post(self._url, json=body, headers=self._headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderError(self.name, type(exc).__name__) from exc
        if resp.status_code in _RETRYABLE:
            raise ProviderError(self.name, f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise ProviderError(self.name, f"HTTP {resp.status_code} (not retryable)")
        return parse_turn(resp.json(), self.name, time.monotonic() - started)


def parse_turn(data: dict[str, Any], model: str, latency: float = 0.0) -> ModelTurn:
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(model, "unexpected response shape") from exc
    calls = tuple(
        ToolCall(
            id=str(c.get("id") or f"call_{i}"),
            name=str((c.get("function") or {}).get("name") or ""),
            arguments=str((c.get("function") or {}).get("arguments") or ""),
        )
        for i, c in enumerate(msg.get("tool_calls") or [])
    )
    usage = data.get("usage") or {}
    return ModelTurn(
        content=(msg.get("content") or "").strip(),
        tool_calls=calls,
        model=str(data.get("model") or model),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        latency_s=latency,
        finish_reason=str(data["choices"][0].get("finish_reason") or ""),
        cached_tokens=int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
    )


@dataclass
class Fallback:
    model: str
    error: str


@dataclass
class GatewayReply:
    turn: ModelTurn
    cost_kobo: int
    fallbacks: list[Fallback] = field(default_factory=list)


class AllModelsFailed(RuntimeError):
    def __init__(self, fallbacks: list[Fallback]) -> None:
        self.fallbacks = fallbacks
        super().__init__("; ".join(f"{f.model}: {f.error}" for f in fallbacks))


class Gateway:
    """Primary model first; on a provider error, the next one, same run."""

    def __init__(self, models: Sequence[ToolModel], *, max_tokens: int = 1200) -> None:
        if not models:
            raise ValueError("Gateway needs at least one model")
        self.models = list(models)
        self.max_tokens = max_tokens

    @property
    def primary(self) -> str:
        return self.models[0].name

    def chat(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> GatewayReply:
        failed: list[Fallback] = []
        for model in self.models:
            try:
                with metering.metered(metering.HARNESS):
                    turn = model.chat(messages, tools, max_tokens=self.max_tokens)
            except ProviderError as exc:
                failed.append(Fallback(model.name, exc.detail))
                continue
            cost = (
                turn.cost_kobo
                if turn.cost_kobo is not None
                else kobo_cost(turn.model, turn.prompt_tokens, turn.completion_tokens,
                               turn.cached_tokens)
            )
            return GatewayReply(turn, cost, failed)
        raise AllModelsFailed(failed)


# ------------------------------------------------------------------ doubles

Script = Callable[[Sequence[dict[str, Any]], Sequence[dict[str, Any]]], ModelTurn]


class ScriptedModel:
    """Deterministic model: ``script(messages, tools)`` gives the next turn.

    ``turns`` may be a list (one per call, in order) or a callable that reads
    the transcript, which is how the scripted eval plays a real agent's part.
    An item that is an exception instance is raised instead (provider errors).
    """

    def __init__(self, turns: Sequence[ModelTurn | Exception] | Script, name: str = "scripted"):
        self.name = name
        self._turns = turns
        self.calls = 0
        self.seen: list[list[dict[str, Any]]] = []

    def chat(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]], *,
        max_tokens: int,
    ) -> ModelTurn:
        metering.guard(self.name)
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        if callable(self._turns):
            item: ModelTurn | Exception = self._turns(messages, tools)
        else:
            idx = self.calls - 1
            if idx >= len(self._turns):
                raise ProviderError(self.name, "script exhausted")
            item = self._turns[idx]
        if isinstance(item, Exception):
            raise item
        return item


def say(text: str, *calls: tuple[str, dict[str, Any] | str], model: str = "scripted",
        cost_kobo: int | None = None) -> ModelTurn:
    """Build a scripted turn: ``say("", ("get_balance", {"shop_id": "shop-001"}))``."""
    out = []
    for i, (name, args) in enumerate(calls):
        raw = args if isinstance(args, str) else json.dumps(args, sort_keys=True)
        out.append(ToolCall(f"call_{i}_{name}", name, raw))
    return ModelTurn(text, tuple(out), model, fake=True, cost_kobo=cost_kobo)


class CacheMiss(KeyError):
    pass


# Per-call versions for a recorded loop call: the messages and tools in, a
# stamp out (``mimi.harness.versions.tool_call_versions``: the Go policy
# version and the calling helper's skill version).
VersionsFor = Callable[[Sequence[dict[str, Any]], Sequence[dict[str, Any]]], Mapping[str, str]]


def _default_versions(messages: Sequence[dict[str, Any]],
                      tools: Sequence[dict[str, Any]]) -> Mapping[str, str]:
    from mimi.harness.versions import tool_call_versions

    return tool_call_versions(messages, tools)


class RecordedToolModel:
    """Record live tool-calling turns by request hash; replay them offline.

    Keys carry the policy and skill versions (RAI-309): an answer recorded
    under one policy or skill version misses under another, counted as
    ``stale``. ``versions=None`` turns the stamp off (plain request keys)."""

    def __init__(self, path: Path, live: ToolModel | None = None, *, name: str = "",
                 versions: VersionsFor | None = _default_versions) -> None:
        self.path = path
        self.live = live
        self.name = name or (live.name if live else "replay")
        self.versions = versions
        self.hits = 0
        self.misses = 0
        self.stale = 0
        self._data: dict[str, Any] = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        )
        self._requests = requests_index(self._data)

    @staticmethod
    def key(name: str, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]) -> str:
        canon = json.dumps({"m": name, "messages": list(messages), "tools": list(tools)},
                           sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()

    def chat(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]], *,
        max_tokens: int,
    ) -> ModelTurn:
        metering.guard(self.name)
        base = self.key(self.name, messages, tools)
        v = dict(self.versions(messages, tools)) if self.versions else {}
        k = versioned_key(base, v)
        if k in self._data:
            self.hits += 1
            return _turn_from(self._data[k])
        if v and os.environ.get("MIMI_CACHE_MAP") and base in self._data \
                and "versions" not in self._data[base]:
            note_migration(self.path, base, k, v)
            self.hits += 1
            return _turn_from(self._data[base])
        stale = bool(v) and base in self._requests
        if self.live is None:
            self.misses += 1
            if stale:
                self.stale += 1
                raise CacheMiss(f"stale: {base} was recorded under {self._requests[base]}, "
                                f"now {v}; re-record")
            raise CacheMiss(k)
        self.stale += stale
        turn = self.live.chat(messages, tools, max_tokens=max_tokens)
        self._data[k] = _turn_to(turn) | ({"request": base, "versions": v} if v else {})
        if v:
            self._requests[base] = v
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: a process killed mid-write (the worker-kill run) must not
        # leave a torn cache behind.
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self._data, sort_keys=True, indent=1) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.path)
        return turn


def _turn_to(t: ModelTurn) -> dict[str, Any]:
    return {"content": t.content, "model": t.model, "prompt_tokens": t.prompt_tokens,
            "completion_tokens": t.completion_tokens, "latency_s": round(t.latency_s, 3),
            "finish_reason": t.finish_reason, "cached_tokens": t.cached_tokens,
            "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments}
                           for c in t.tool_calls]}


def _turn_from(d: dict[str, Any]) -> ModelTurn:
    return ModelTurn(
        content=d["content"],
        tool_calls=tuple(ToolCall(c["id"], c["name"], c["arguments"]) for c in d["tool_calls"]),
        model=d["model"],
        prompt_tokens=int(d["prompt_tokens"]),
        completion_tokens=int(d["completion_tokens"]),
        latency_s=float(d.get("latency_s", 0.0)),
        finish_reason=str(d.get("finish_reason", "")),
        cached_tokens=int(d.get("cached_tokens", 0)),
    )
