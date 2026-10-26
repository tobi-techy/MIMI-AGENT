"""One POST with a timeout and one retry, shared by the speech adapters.

Retried once: a timeout, a connection error, 429 or 5xx (after
``Retry-After`` when the server sends one, capped at 5 seconds). Anything
else fails at once. Keys go in headers built by the caller and are never put
in an error message.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from mimi.voice.base import TranscriptionError

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def post_with_retry(
    client: httpx.Client,
    url: str,
    *,
    label: str,
    retries: int = 1,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> tuple[httpx.Response, float]:
    """(the 200 response, its latency in seconds) or ``TranscriptionError``."""
    last = ""
    for attempt in range(retries + 1):
        started = time.monotonic()
        try:
            resp = client.post(url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = type(exc).__name__
            wait = 1.0 + attempt
        else:
            if resp.status_code == 200:
                return resp, time.monotonic() - started
            last = f"HTTP {resp.status_code}{_detail(resp)}"
            if resp.status_code not in RETRY_STATUS:
                break
            wait = _retry_after(resp.headers, 1.0 + attempt)
        if attempt < retries:
            sleep(wait)
    raise TranscriptionError(f"{label} failed: {last}")


def _retry_after(headers: Mapping[str, str], default: float) -> float:
    try:
        return min(float(headers.get("retry-after", default)), 5.0)
    except ValueError:
        return default


def _detail(resp: httpx.Response) -> str:
    """The API's client-safe error message, shortened (never request data)."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    msg: object = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code") or ""
        elif isinstance(body.get("detail"), str):
            msg = body["detail"]
    text = " ".join(str(msg).split())[:160]
    return f": {text}" if text else ""
