"""Pick the model for a role from the environment.

* ``META_API_KEY``: Meta Model API key. Unset or empty means FakeModel.
* ``META_API_BASE``: base URL, default ``https://api.meta.ai/v1``.
* ``MIMI_MODEL``: Mimi's model, default ``muse-spark-1.3``.
* ``SIM_MODEL``: the shop simulator's model, default
  ``muse-spark-1.3-contributor``.
* ``META_TIMEOUT_S``: per-call timeout, default 60.
* ``META_REASONING_EFFORT``: ``minimal`` / ``low`` (default) / ``medium`` ...

Keys are read here and passed to the client; nothing else touches them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from mimi.models.base import ChatModel
from mimi.models.fake import FakeModel, Responder
from mimi.models.meta import DEFAULT_BASE, MetaChatModel

ModelRole = Literal["mimi", "sim"]
DEFAULT_MODELS: dict[str, str] = {
    "mimi": "muse-spark-1.3",
    "sim": "muse-spark-1.3-contributor",
}
_MODEL_VAR = {"mimi": "MIMI_MODEL", "sim": "SIM_MODEL"}


def has_key(env: Mapping[str, str] | None = None) -> bool:
    return bool(
        (env if env is not None else os.environ).get("META_API_KEY", "").strip()
    )


def from_env(
    role: ModelRole,
    *,
    fake: Responder | None = None,
    env: Mapping[str, str] | None = None,
) -> ChatModel:
    e = env if env is not None else os.environ
    model = e.get(_MODEL_VAR[role], "").strip() or DEFAULT_MODELS[role]
    key = e.get("META_API_KEY", "").strip()
    if not key:
        return (
            FakeModel(fake, name=f"fake:{role}")
            if fake
            else FakeModel(name=f"fake:{role}")
        )
    return MetaChatModel(
        api_key=key,
        model=model,
        base_url=e.get("META_API_BASE", "").strip() or DEFAULT_BASE,
        timeout_s=float(e.get("META_TIMEOUT_S", "60") or 60),
        reasoning_effort=e.get("META_REASONING_EFFORT", "low").strip() or None,
    )
