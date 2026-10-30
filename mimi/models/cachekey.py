"""``prompt_cache_key``: one stable key per shared prompt prefix (Meta prompt caching).

Meta's Model API caches prompt prefixes on its own; at volume a stable
``prompt_cache_key`` routes requests that share a prefix to the same backend,
so more of them hit (https://dev.meta.ai/docs/prompt-caching: "one stable key
per shared prefix ... not a per-user or per-request value").

The key names the helper (or role) and hashes what the request's stable prefix
is made of: the system prompt (core rules, ``# Helper:`` and its skill, so the
prompt version) and the tool schemas. Per-thread, per-shop and per-turn text
never goes into it.

It goes in the request body only. The record/replay keys
(``RecordedToolModel.key``, ``mimi.models.cache.cache_key``) hash the messages
and tools, never the body, so recorded answers replay unchanged.
``MIMI_PROMPT_CACHE_KEY=0`` stops sending it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from typing import Any

_HELPER = re.compile(r"^# Helper: ([a-z0-9_/-]+)\s*$", re.M)


def enabled() -> bool:
    return os.environ.get("MIMI_PROMPT_CACHE_KEY", "1").strip() != "0"


def _system(messages: Sequence[Any]) -> str:
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", "")
        if role in ("system", "developer"):
            content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            return content if isinstance(content, str) else json.dumps(content, sort_keys=True)
    return ""


def prompt_cache_key(messages: Sequence[Any], tools: Sequence[Any] = (), *,
                     role: str = "") -> str:
    """``mimi-<helper or role>-<hash of system prompt and tools>``, or "" when the
    request has no stable system prompt to share."""
    system = _system(messages)
    if not system:
        return ""
    found = _HELPER.search(system)
    who = (found.group(1) if found else role or "chat").replace("/", "-")
    digest = hashlib.sha256(json.dumps({"system": system, "tools": list(tools)},
                                       sort_keys=True, separators=(",", ":"))
                            .encode()).hexdigest()[:16]
    return f"mimi-{who}-{digest}"
