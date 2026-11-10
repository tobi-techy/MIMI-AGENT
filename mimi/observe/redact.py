"""Redaction for anything that leaves the machine in a trace.

Every value the Langfuse exporter sends goes through ``redact`` first (and
the client's own ``mask`` hook runs it again). The data is synthetic, but the
rules are the ones a real shop's data would need:

* phone numbers: ``+234 803 123 4567`` -> ``+234******4567``; local
  ``0803 123 4567`` -> ``0*******4567`` (the last 4 digits stay);
* account numbers and any other run of 10 or more digits: only the last 4
  digits stay (``******6789``), wherever the run sits, except inside a digest
  the exporter itself wrote (see below);
* keys and tokens: ``Bearer ...``, ``sk-...``, ``pk-lf-...``/``sk-lf-...``,
  ``key=...`` / ``secret: ...`` / ``password=...`` -> ``[redacted]``;
* images and audio: raw bytes and ``data:...;base64,...`` URLs never leave;
  they become ``{"sha256": ..., "bytes": n}`` / ``[image sha256=...]``, and a
  64-character hex digest is kept digit for digit;
* fields named like ``account``, ``phone``, ``msisdn``, ``sender_phone``:
  digits masked to the last 4 whatever their shape;
* integers of 10 or more digits (a phone or account number stored as a
  number) are masked the same way, except in fields that are clearly a
  measure (``*_kobo``, ``*_ms``, ``*_ns``, ``*_tokens``, ``*_bytes``,
  ``ts``, ``seq``...).

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
# Any run of 10 or more digits (account numbers, NIP session ids, ...),
# whatever surrounds it: a run glued to a letter ("acct1234567890") is the same
# identifier and must not leak (AGENTS.md: no long digit run in a trace).
_LONG_DIGITS = re.compile(r"(?<!\d)\d{10,}(?!\d)")
# A digest the exporter itself wrote (``blob``, ``_data_url``): a redaction
# output, not data of record, so the run inside it is left alone and a trace's
# hash still matches the record it came from. Exactly 64 hex characters, so no
# identifier can pass as one ("1234567890" is one 10-digit run, not a digest).
_HEX_DIGEST = re.compile(r"(?<![\w])[0-9a-f]{64}(?!\w)", re.I)
# Our own correlation ids (``mimi.observe.correlation``): a hex digest that
# may contain a run of 10 digits by chance. It names a trace, not a person, so
# the leak check ignores it (masking it would break correlation in a log line).
_OUR_IDS = re.compile(r"\b(?:turn|tr)_[0-9a-f]{16,40}\b")


def _long_digits(text: str) -> str:
    """Mask every run of 10 or more digits, except inside a digest or our id."""
    spans = [m.span() for m in _HEX_DIGEST.finditer(text)]
    spans += [m.span() for m in _OUR_IDS.finditer(text)]
    if not spans:
        return _LONG_DIGITS.sub(lambda m: mask_digits(m.group(0)), text)

    def one(m: re.Match[str]) -> str:
        s, e = m.span()
        if any(a <= s and e <= b for a, b in spans):
            return m.group(0)
        return mask_digits(m.group(0))

    return _LONG_DIGITS.sub(one, text)
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
# Field names whose integers are measures, never identifiers.
_MEASURE_SUFFIXES = ("_kobo", "_ms", "_ns", "_s", "_tokens", "_bytes", "_ts", "_count",
                     "_at", "_unix", "_epoch")
_MEASURE_KEYS = frozenset({"ts", "seq", "bytes", "tokens", "kobo", "duration", "input",
                           "output", "total", "step", "steps", "epoch"})
_LONG_INT = 10 ** 9  # 10 or more digits


def _is_measure(key: str) -> bool:
    k = key.lower()
    return k in _MEASURE_KEYS or k.endswith(_MEASURE_SUFFIXES)


def _long_int(value: int) -> bool:
    return abs(value) >= _LONG_INT


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def mask_phone(raw: str) -> str:
    if "*" in raw:
        # Already masked by an earlier pass: redaction is idempotent, so a
        # sink may redact a trace the guard already redacted.
        return raw
    d = _digits(raw)
    if d.startswith("234"):
        return "+234" + "*" * max(0, len(d) - 7) + d[-4:]
    return d[:1] + "*" * max(0, len(d) - 5) + d[-4:]


def mask_digits(raw: str) -> str:
    if "*" in raw:
        return raw
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
    out = _long_digits(out)
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
    if isinstance(value, int) and not isinstance(value, bool) and _is_measure(k):
        return value
    return redact(value, depth + 1)


def redact(value: Any, _depth: int = 0) -> Any:
    """A copy of ``value`` safe to export (see the module doc)."""
    try:
        if _depth > 12:
            return "[truncated]"
        if isinstance(value, int) and not isinstance(value, bool) and _long_int(value):
            # A phone or account number kept as a number (see the module doc).
            return mask_digits(str(value))
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


# ------------------------------------------------------------- the leak check


def _leaks_in_text(text: str) -> list[str]:
    out: list[str] = []
    if _PHONE_INTL.search(text) or _PHONE_LOCAL.search(text):
        out.append("phone")
    scan = _OUR_IDS.sub(" ", text)
    spans = [m.span() for m in _HEX_DIGEST.finditer(scan)]
    for m in _LONG_DIGITS.finditer(scan):
        s, e = m.span()
        if not any(a <= s and e <= b for a, b in spans):
            out.append("long digits")
            break
    if any(pat.search(text.replace(REDACTED, "")) for pat in _KEYS) or _DATA_URL.search(text):
        out.append("key or media")
    return out


def find_leaks(value: Any, *, path: str = "", _depth: int = 0) -> list[str]:
    """Where an **already redacted** value still looks private: field paths.

    Empty is the only acceptable answer before data leaves the process
    (``mimi.observe.sinks``). The list names the field and the kind of data
    (``"input.text: phone"``), never the data itself: a leak report that
    carried the leak would be one more leak.
    """
    try:
        if _depth > 12:
            return []
        if isinstance(value, int) and not isinstance(value, bool):
            if _long_int(value) and not _is_measure(path.rsplit(".", 1)[-1]):
                return [f"{path or 'value'}: long number"]
            return []
        if value is None or isinstance(value, bool | float):
            return []
        if isinstance(value, bytes | bytearray | memoryview):
            return [f"{path or 'value'}: raw bytes"]
        if isinstance(value, str):
            return [f"{path or 'value'}: {kind}" for kind in _leaks_in_text(value)]
        if isinstance(value, Mapping):
            out: list[str] = []
            for k, v in value.items():
                key = str(k)
                out.extend(find_leaks(v, path=f"{path}.{key}" if path else key,
                                      _depth=_depth + 1))
            return out
        if isinstance(value, Sequence | set | frozenset):
            out = []
            for i, v in enumerate(sorted(value, key=str) if isinstance(value, set | frozenset)
                                  else value):
                out.extend(find_leaks(v, path=f"{path}[{i}]", _depth=_depth + 1))
            return out
        return _leaks_in_text(str(value))
    except Exception:  # a check that fails closed: count it as a leak
        return [f"{path or 'value'}: unreadable"]
