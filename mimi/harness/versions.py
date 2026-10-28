"""The versions a recorded model answer depends on (RAI-309).

A model cache key is the request hash. Some changes alter what a recorded
answer should be without changing the request text: a tier change in Go
policy, a skill bumped to a new version with the same words. So the loop's
recorded calls are stamped with ``tool_call_versions`` and the router's with
``ROUTER_VERSIONS``; an answer replays only under the stamp it was recorded
with (``mimi.models.cache.versioned_key``).

Helpers register themselves when they are built (``register``), keyed by
their exact system prompt, so a call is matched to its helper by the system
message it carries. Two helpers that share a name (the Order receipt helper
and Order intake) have different prompts, so they keep their own versions.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from mimi.harness.policy import POLICY_VERSION
from mimi.harness.router import ROUTER_PROMPT_VERSION
from mimi.harness.skills import CATALOGUE_VERSION

# The router never reads Go policy: its answers depend on its prompt and the
# skill catalogue it routes over.
ROUTER_VERSIONS: dict[str, str] = {"router_prompt": ROUTER_PROMPT_VERSION,
                                   "catalogue": CATALOGUE_VERSION}

_lock = threading.Lock()
_by_prompt: dict[str, str] = {}
_HELPER = re.compile(r"^# Helper: (\S+)$", re.M)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def register(helper: Any) -> Any:
    """Remember ``helper``'s skill version under its system prompt; returns it."""
    key = _h(helper.system_prompt())
    stamp = f"{helper.name}:{helper.skill_version}"
    with _lock:
        old = _by_prompt.get(key)
        if old and old != stamp:
            # One prompt under two versions: keep both, in a fixed order.
            stamp = "|".join(sorted(set(old.split("|")) | {stamp}))
        _by_prompt[key] = stamp
    return helper


def skill_stamp(system: str) -> str:
    with _lock:
        hit = _by_prompt.get(_h(system))
    if hit:
        return hit
    m = _HELPER.search(system)
    return f"{m.group(1) if m else '-'}:unregistered"


def tool_call_versions(messages: Sequence[dict[str, Any]],
                       tools: Sequence[dict[str, Any]] = ()) -> Mapping[str, str]:
    system = ""
    if messages and messages[0].get("role") == "system":
        system = str(messages[0].get("content") or "")
    return {"policy": POLICY_VERSION, "skill": skill_stamp(system)}


__all__ = ["ROUTER_VERSIONS", "register", "skill_stamp", "tool_call_versions"]
