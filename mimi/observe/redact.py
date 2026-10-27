"""Redaction for anything that leaves the machine in a trace.

Every value the Langfuse exporter sends goes through ``redact`` first (and
the client's own ``mask`` hook runs it again). The data is synthetic, but the
rules are the ones a real shop's data would need:

* phone numbers: ``+234 803 123 4567`` -> ``+234******4567``; local
  ``0803 123 4567`` -> ``0*******4567`` (the last 4 digits stay);
* account numbers and any other run of 10 or more digits: only the last 4
  digits stay (``******6789``);
* keys and tokens: ``Bearer ...``, ``sk-...``, ``pk-lf-...``/``sk-lf-...``,
  ``key=...`` / ``secret: ...`` / ``password=...`` -> ``[redacted]``;
* images and audio: raw bytes and ``data:...;base64,...`` URLs never leave;
  they become ``{"sha256": ..., "bytes": n}`` / ``[image sha256=...]``;
* fields named like ``account``, ``phone``, ``msisdn``, ``sender_phone``:
  digits masked to the last 4 whatever their shape.

``redact`` is pure and total: it never raises, whatever it is given.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[redacted]"

# +234 followed by 10 digits, with optional spaces or dashes between groups.
_PHONE_INTL = re.compile(r"\+?\b234[\s-]?(?:\d[\s-]?){9}\d\b")
# Nigerian local mobile numbers: 0 then 7/8/9 then 9 more digits.
_PHONE_LOCAL = re.compile(r"(?<![\w+])0[789](?:[\s-]?\d){9}(?!\w)")
# Any run of 10 or more digits (account numbers, NIP session ids, ...).
_LONG_DIGITS = re.compile(r"(?<![\w])\d{10,}(?![\w])")
_DATA_URL = re.compile(r"data:(?P<mime>[\w/+.-]+);base64,(?P<b64>[A-Za-z0-9+/=]+)")
_KEYS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b(?:pk|sk)-lf-[A-Za-z0-9-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|secret(?:[_-]?key)?|token|password|authorization)"
        r"(\s*[:=]\s*)(\"?)[^\s\"',;]+"
    ),
)
_PASS_THROUGH = frozenset({"LangfuseMedia"})
_SENSITIVE_KEYS = ("account", "phone", "msisdn", "nuban")
_SECRET_KEYS = ("api_key", "apikey", "secret", "password", "token", "authorization")


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def mask_phone(raw: str) -> str:
    d = _digits(raw)
    if d.startswith("234"):
        return "+234" + "*" * max(0, len(d) - 7) + d[-4:]
    return d[:1] + "*" * max(0, len(d) - 5) + d[-4:]


def mask_digits(raw: str) -> str:
    d = _digits(raw)
    if len(d) <= 4:
        return "*" * len(d)
    return "*" * (len(d) - 4) + d[-4:]


def _data_url(m: re.Match[str]) -> str:
    try:
        raw = base64.b64decode(m.group("b64"), validate=False)
    except (binascii.Error, ValueError):
        raw = m.group("b64").encode()
    return f"[{m.group('mime')} sha256={hashlib.sha256(raw).hexdigest()} bytes={len(raw)}]"


def redact_text(text: str) -> str:
    out = _DATA_URL.sub(_data_url, text)
    for pat in _KEYS:
        out = pat.sub(
            lambda m: (m.group(1) + m.group(2) + REDACTED) if m.lastindex else REDACTED, out
        )
    out = _PHONE_INTL.sub(lambda m: mask_phone(m.group(0)), out)
    out = _PHONE_LOCAL.sub(lambda m: mask_phone(m.group(0)), out)
    out = _LONG_DIGITS.sub(lambda m: mask_digits(m.group(0)), out)
    return out


def blob(data: bytes | bytearray | memoryview) -> dict[str, Any]:
    raw = bytes(data)
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _field(key: str, value: Any, depth: int = 0) -> Any:
    k = key.lower()
    if any(s in k for s in _SECRET_KEYS) and isinstance(value, str | bytes) and value:
        return REDACTED
    if any(s in k for s in _SENSITIVE_KEYS) and isinstance(value, str | int) and value != "":
        s = str(value)
        if len(_digits(s)) >= 5:
            return mask_phone(s) if "phone" in k or "msisdn" in k else mask_digits(s)
    return redact(value, depth + 1)


def redact(value: Any, _depth: int = 0) -> Any:
    """A copy of ``value`` safe to export (see the module doc)."""
    try:
        if _depth > 12:
            return "[truncated]"
        if value is None or isinstance(value, bool | int | float):
            return value
        if type(value).__name__ in _PASS_THROUGH:
            # Synthetic media the exporter chose to attach (see langfuse_export).
            return value
        if isinstance(value, bytes | bytearray | memoryview):
            return blob(value)
        if isinstance(value, str):
            return redact_text(value)
        if isinstance(value, Mapping):
            return {str(k): _field(str(k), v, _depth) for k, v in value.items()}
        if isinstance(value, Sequence):
            return [redact(v, _depth + 1) for v in value]
        if isinstance(value, set | frozenset):
            return [redact(v, _depth + 1) for v in sorted(value, key=str)]
        return redact_text(str(value))
    except Exception:  # never let redaction break a turn; send nothing instead
        return REDACTED
