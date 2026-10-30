"""OpenAI-compatible Chat Completions client for Meta's Model API.

Plain httpx, no SDK: one POST to ``{base}/chat/completions``, a timeout, and
one retry on a timeout, a connection error, 429 or 5xx. Muse Spark is a
reasoning model, so this sends no ``temperature`` or ``stop`` (the docs advise
default sampling and reject ``stop``) and uses ``max_completion_tokens``.
A message with images is sent as content parts (``text`` then ``image_url``
with a base64 data URL), the OpenAI-compatible vision shape Muse Spark takes.
The key comes from the caller (``env.from_env`` reads ``META_API_KEY``); it is
never logged or put in an error message.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from mimi.models import cachekey, metering
from mimi.models.base import ChatMessage, ChatResult, ModelError

DEFAULT_BASE = "https://api.meta.ai/v1"
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def _message(m: ChatMessage) -> dict[str, Any]:
    """A plain text message, or text plus images as content parts (vision)."""
    if not m.images:
        return {"role": m.role, "content": m.content}
    parts: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
    parts += [{"type": "image_url", "image_url": {"url": im.data_url()}} for im in m.images]
    return {"role": m.role, "content": parts}


class MetaChatModel:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_BASE,
        timeout_s: float = 60.0,
        retries: int = 1,
        reasoning_effort: str | None = "low",
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("MetaChatModel needs an API key")
        self.name = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = timeout_s
        self._retries = retries
        self._effort = reasoning_effort
        self._client = httpx.Client(timeout=timeout_s, transport=transport)
        self._sleep = sleep
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int = 400,
        meta: Mapping[str, str] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> ChatResult:
        metering.guard(self.name)
        body: dict[str, Any] = {
            "model": self.name,
            "messages": [_message(m) for m in messages],
            "max_completion_tokens": max_tokens,
        }
        if self._effort:
            body["reasoning_effort"] = self._effort
        if response_format:
            body["response_format"] = dict(response_format)
        if cachekey.enabled():
            key = cachekey.prompt_cache_key(messages)
            if key:
                body["prompt_cache_key"] = key
        last: str = ""
        for attempt in range(self._retries + 1):
            started = time.monotonic()
            try:
                resp = self._client.post(self._url, json=body, headers=self._headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = type(exc).__name__
            else:
                if resp.status_code == 200:
                    return self._result(resp.json(), time.monotonic() - started)
                last = f"HTTP {resp.status_code}"
                if resp.status_code not in _RETRY_STATUS:
                    break
            if attempt < self._retries:
                self._sleep(1.0 + attempt)
        raise ModelError(f"{self.name} call failed: {last}")

    def _result(self, data: dict[str, Any], latency: float) -> ChatResult:
        self.calls += 1
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"{self.name}: unexpected response shape") from exc
        usage = data.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        self.prompt_tokens += pt
        self.completion_tokens += ct
        return ChatResult(
            text=text.strip(),
            model=str(data.get("model") or self.name),
            prompt_tokens=pt,
            completion_tokens=ct,
            latency_s=latency,
            raw_usage=usage,
        )
