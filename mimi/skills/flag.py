"""The ``MIMI_SKILLS`` flag (default off).

Off: every helper runs exactly as before, and every eval replays its
recorded answers unchanged. On: helpers are bound per skill
(``mimi.skills.bind``), and the evals that run the agent record and replay
their model answers in a separate ``*.skills.cache.json`` file beside the
flag-off one, so the two modes never mix.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

FLAG = "MIMI_SKILLS"


def enabled(env: Mapping[str, str] | None = None) -> bool:
    e = os.environ if env is None else env
    return e.get(FLAG, "").strip().lower() in ("1", "true", "on", "yes")


SUFFIXES = (".cache.json", ".report.json", ".report.md")


def cache_path(path: Path, env: Mapping[str, str] | None = None) -> Path:
    """The skills-mode twin of a recorded-answer cache or an eval report
    (``x.cache.json`` -> ``x.skills.cache.json``); the same path when off."""
    if not enabled(env):
        return path
    for suf in SUFFIXES:
        if path.name.endswith(suf):
            return path.with_name(path.name[: -len(suf)] + ".skills" + suf)
    return path
