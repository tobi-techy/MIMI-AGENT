"""The MIMI_CHECKIN flag: 0 (off, default), quiet (store counts, no restock
offer), 1 (on). Read from the environment, decided in code."""

from __future__ import annotations

import os
from typing import Literal

CheckinMode = Literal["off", "quiet", "on"]

ENV_VAR = "MIMI_CHECKIN"


def mode_from_env(env: dict[str, str] | None = None) -> CheckinMode:
    raw = (env if env is not None else os.environ).get(ENV_VAR, "0").strip().lower()
    if raw in ("1", "on", "yes", "true"):
        return "on"
    if raw in ("quiet", "store"):
        return "quiet"
    return "off"


def checkin_mode(env: dict[str, str] | None = None) -> CheckinMode:
    return mode_from_env(env)


__all__ = ["ENV_VAR", "CheckinMode", "checkin_mode", "mode_from_env"]
