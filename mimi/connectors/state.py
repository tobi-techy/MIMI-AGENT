"""Signed single-use OAuth state for the connect flow (RAI-277)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

STATE_TTL_S = 30 * 60  # the connect state expires in 30 minutes


def sign_state(secret: str, payload: dict[str, Any], *, now: float | None = None) -> str:
    """Sign ``payload`` with ``secret``. The state carries its own time."""
    body = base64.urlsafe_b64encode(json.dumps(
        {**payload, "at": int(now if now is not None else time.time())},
        sort_keys=True).encode()).decode()
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def check_state(secret: str, state: str, *, now: float | None = None) -> dict[str, Any]:
    """Verify a connect state. Raises ``ValueError`` when it is bad,
    expired or reused (reuse is tracked by the caller marking it used)."""
    try:
        body, sig = state.rsplit(".", 1)
    except ValueError:
        raise ValueError("that link is not valid; start a new connect") from None
    want = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig):
        raise ValueError("that link is not valid; start a new connect")
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode()).decode())
    except (ValueError, UnicodeDecodeError):
        raise ValueError("that link is not valid; start a new connect") from None
    at = int(payload.get("at", 0))
    here = int(now if now is not None else time.time())
    if here - at > STATE_TTL_S:
        raise ValueError("that link expired; start a new connect")
    if at - here > 60:
        raise ValueError("that link is not valid; start a new connect")
    payload.pop("at", None)
    return payload
