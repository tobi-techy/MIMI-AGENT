"""Outbound intents with idempotency keys, and the sink that hands them to Go.

The key is derived from the job, the step and the intent's own content, so a
resumed job that re-proposes the same intent produces the same key, and Go's
dedupe (or the fake sink here) turns the second submit into a no-op.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from decimal import Decimal
from typing import Any, Protocol

# The runtime starts with no intent types of its own. Each helper registers
# the typed proposals it may send with ``intent_types`` (the receipt flow does
# in ``mimi.receipts.intents``), so a runner only ever accepts what its helper
# declared. None of them moves money.
ALLOWED_INTENT_TYPES: tuple[type, ...] = ()


# Words no intent kind may carry. An extension type (see ``intent_types``)
# is checked against these when a runner is built, so a helper cannot register
# a money mover under a new name.
MONEY_VERBS: tuple[str, ...] = (
    "transfer",
    "payout",
    "pay_out",
    "send_money",
    "send_funds",
    "withdraw",
    "disburse",
    "refund",
    # The outside review (Medium): payment words a new tool could hide behind.
    "wire",
    "settle",
    "remit",
    "debit",
)


class ForbiddenIntent(TypeError):
    """A step returned something that is not a typed intent."""


def intent_types(*extra: type) -> tuple[type, ...]:
    """The intent types a runner accepts: a helper's own typed proposals.

    Each type must be a frozen dataclass with a string ``kind`` default that
    names no money verb. An intent is a proposal the system of record
    validates; it never acts.
    """
    for cls in extra:
        params = getattr(cls, "__dataclass_params__", None)
        if not dataclasses.is_dataclass(cls) or not getattr(params, "frozen", False):
            raise ForbiddenIntent(f"{cls.__name__} must be a frozen dataclass")
        kind_field = next(
            (f for f in dataclasses.fields(cls) if f.name == "kind"), None
        )
        kind = getattr(kind_field, "default", None)
        if not isinstance(kind, str) or not kind:
            raise ForbiddenIntent(f"{cls.__name__} has no kind")
        if any(verb in kind for verb in MONEY_VERBS):
            raise ForbiddenIntent(f"{cls.__name__} kind {kind!r} moves money")
    return ALLOWED_INTENT_TYPES + tuple(extra)


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def intent_payload(
    intent: object, allowed: tuple[type, ...] = ALLOWED_INTENT_TYPES
) -> tuple[str, dict[str, Any]]:
    # The trailing ``type`` check narrows for mypy: is_dataclass is also true
    # of a dataclass class, and a class is not an intent.
    if (
        not isinstance(intent, allowed)
        or not dataclasses.is_dataclass(intent)
        or isinstance(intent, type)
    ):
        raise ForbiddenIntent(
            f"{type(intent).__name__} is not a typed intent; steps propose, "
            "they never act"
        )
    payload = _plain(dataclasses.asdict(intent))
    kind = str(payload.pop("kind"))
    return kind, payload


def idempotency_key(
    job_id: str, step_index: int, position: int, kind: str, payload: dict[str, Any]
) -> str:
    canon = json.dumps(
        {"job": job_id, "step": step_index, "i": position, "kind": kind, "p": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "rt_" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]


class IntentSink(Protocol):
    """Go's intent API. Must dedupe on the key: same key, same receipt."""

    def submit(
        self, tenant_id: str, idempotency_key: str, kind: str, payload: dict[str, Any]
    ) -> str: ...


class RecordingIntentSink:
    """In-memory Go stand-in that dedupes exactly as Go must."""

    def __init__(self) -> None:
        self.accepted: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self.calls = 0

    def submit(
        self, tenant_id: str, idempotency_key: str, kind: str, payload: dict[str, Any]
    ) -> str:
        self.calls += 1
        self.accepted.setdefault(idempotency_key, (tenant_id, kind, payload))
        return idempotency_key
