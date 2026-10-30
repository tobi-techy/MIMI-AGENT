"""Every price Mimi's cost accounting uses, in one place.

Source: Meta Model API pricing, https://dev.meta.ai/docs/pricing-rate-limits
(read 29 Oct 2026). Muse Spark Standard bills $1.25 per 1M input tokens,
$0.15 per 1M **cached** input tokens (automatic prefix caching,
https://dev.meta.ai/docs/prompt-caching) and $4.25 per 1M output tokens
(hidden reasoning is billed as output). Contributor mode is $0.10 / $0.002 /
$0.20. SAM 3.1 is $2.50 per 1,000 images. Muse Voice Transcribe is $0.18 an
hour; Spitch speech to text is $0.00042 a second (docs/VOICE.md, 26 Oct 2026).

Naira: a planning figure, not a quote (budgets are set in naira, the API bills
in dollars): 1 USD = N1,600 = 160,000 kobo, override with ``MIMI_USD_NGN``.

``MIMI_CACHED_RATE=0`` prices cached input at the full input rate (the
accounting before 29 Oct 2026); default on.
"""

from __future__ import annotations

import os
from typing import NamedTuple


class TokenPrice(NamedTuple):
    """USD per 1M tokens."""

    input: float
    output: float
    cached_input: float


TOKEN_PRICES: dict[str, TokenPrice] = {
    "muse-spark-1.3": TokenPrice(1.25, 4.25, 0.15),
    "muse-spark-1.2": TokenPrice(1.25, 4.25, 0.15),
    "muse-spark-1.1": TokenPrice(1.25, 4.25, 0.15),
    "muse-spark-1.3-contributor": TokenPrice(0.10, 0.20, 0.002),
    "muse-spark-1.2-contributor": TokenPrice(0.10, 0.20, 0.002),
}
# USD per second of audio, by provider prefix.
AUDIO_PRICES: dict[str, float] = {"spitch": 0.00042, "muse": 0.18 / 3600}
SAM_USD_PER_IMAGE = 0.0025

DEFAULT_USD_NGN = 1_600


def usd_to_kobo_rate() -> int:
    raw = os.environ.get("MIMI_USD_NGN", "").strip()
    return int(raw) * 100 if raw.isdigit() else DEFAULT_USD_NGN * 100


def cached_rate_on() -> bool:
    return os.environ.get("MIMI_CACHED_RATE", "1").strip() != "0"


def _micro_usd(model: str, prompt_tokens: int, completion_tokens: int,
               cached_tokens: int, cached_rate: bool | None) -> float | None:
    price = TOKEN_PRICES.get(model)
    if price is None:
        return None
    use_cached = cached_rate_on() if cached_rate is None else cached_rate
    cached = max(0, min(int(cached_tokens), int(prompt_tokens))) if use_cached else 0
    return ((prompt_tokens - cached) * price.input + cached * price.cached_input
            + completion_tokens * price.output)


def token_usd(model: str, prompt_tokens: int, completion_tokens: int,
              cached_tokens: int = 0, *, cached_rate: bool | None = None) -> float | None:
    """USD for one call; ``None`` for an unpriced (fake) model.

    ``cached_tokens`` is the part of ``prompt_tokens`` the provider served from
    its prompt cache; it is billed at the cached rate (unless
    ``MIMI_CACHED_RATE=0``)."""
    micro = _micro_usd(model, prompt_tokens, completion_tokens, cached_tokens, cached_rate)
    return None if micro is None else micro / 1e6


def kobo_cost(model: str, prompt_tokens: int, completion_tokens: int,
              cached_tokens: int = 0, *, cached_rate: bool | None = None) -> int:
    """Integer kobo for one call, rounded up; 0 for an unpriced (fake) model."""
    micro = _micro_usd(model, prompt_tokens, completion_tokens, cached_tokens, cached_rate)
    if micro is None:
        return 0
    kobo = micro * usd_to_kobo_rate() / 1e6
    return int(-(-kobo // 1))


def sam_kobo(images: int, usd_ngn: int = DEFAULT_USD_NGN) -> int:
    """Integer kobo for ``images`` SAM 3.1 images, rounded up."""
    kobo = max(0, images) * SAM_USD_PER_IMAGE * usd_ngn * 100
    return int(-(-kobo // 1))


def audio_usd(provider: str, seconds: float | None) -> float | None:
    key = next((k for k in AUDIO_PRICES if provider.startswith(k)), None)
    if key is None or not seconds:
        return None
    return seconds * AUDIO_PRICES[key]


def audio_kobo(provider: str, seconds: float | None) -> int:
    usd = audio_usd(provider, seconds)
    if usd is None:
        return 0
    kobo = usd * usd_to_kobo_rate()
    return int(-(-kobo // 1))
