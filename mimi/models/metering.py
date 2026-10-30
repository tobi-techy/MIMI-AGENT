"""The one metering seam every paid model call goes through.

Every call Mimi pays for (the harness loop, the router's model layer, the
steer and binding classifiers, the screenshot, receipt, list-photo and stock
readers, SAM 3.1, speech to text) is **charged** here, priced from
``mimi.models.prices``:

* ``Metered`` wraps a chat model (``complete``), ``MeteredTool`` a tool model
  (``chat``), ``MeteredSegmenter`` SAM and ``MeteredTranscriber`` speech to
  text. Each runs its inner client inside ``metered(role)`` and then calls
  ``charge``. The harness ``Gateway`` does the same for the loop.
* ``charge`` prices the call, hands it to the active ``Scope`` (the agent opens
  one per turn) and to every listener (the e2e usage log).
* A ``Scope`` books each charge to the distributor's spend table through
  ``book`` (the only code that calls ``add_spend``), so the daily naira cap
  counts it, and keeps the turn's tally per role, so the per-run budget does.

No call path can bypass it: every client (live, recorded, scripted or fake)
calls ``guard`` on entry. Inside a strict scope a call that did not come
through ``metered`` raises ``UnmeteredCall``; strict is on while the agent
processes a turn under CI (``CI=1``) or ``MIMI_METER_STRICT=1``. Outside it, an
unmetered call is logged and counted (``violations``) so it still shows.

``MIMI_METER_ALL=0`` keeps the accounting before 29 Oct 2026: only the harness
loop is booked to the spend table and the run budget (every call is still
priced and tallied for the reports).
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from mimi.models.prices import audio_kobo, kobo_cost, sam_kobo

log = logging.getLogger(__name__)

# Roles, for the tallies and the reports.
HARNESS = "harness"
ROUTER = "router"
BINDING = "binding"
STEER = "steer"
VISION = "vision"  # payment screenshots and order list photos
JUDGE = "judge"  # receipt reply verdicts
STOCK = "stock"  # worded stock answers
SAM = "sam"
STT = "stt"
CODE = "code"  # a plain-code path stood in for the model (``mimi.harness.plain``): N0
CLASSIFIERS = frozenset({ROUTER, BINDING, STEER})


class UnmeteredCall(RuntimeError):
    """A model client was called outside the metering seam."""


def meter_all() -> bool:
    return os.environ.get("MIMI_METER_ALL", "1").strip() != "0"


def strict_default() -> bool:
    raw = os.environ.get("MIMI_METER_STRICT", "").strip()
    if raw:
        return raw != "0"
    return os.environ.get("CI", "").strip().lower() in ("1", "true")


@dataclass(frozen=True)
class Charge:
    role: str
    model: str
    kobo: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    units: float = 0.0  # SAM images or audio seconds
    booked: bool = False  # already booked by its caller (the harness loop)


class SpendBook(Protocol):
    def add_spend(self, tenant_id: str, helper: str, day: date, kobo: int) -> None: ...


def book(control: SpendBook, tenant_id: str, helper: str, day: date, kobo: int) -> None:
    """The one place model spend reaches the spend table (the daily cap)."""
    if kobo > 0:
        control.add_spend(tenant_id, helper, day, kobo)


@dataclass
class Scope:
    """One turn's metering: books every charge and keeps the tally."""

    control: SpendBook | None = None
    tenant_id: str = ""
    helper: str = ""
    day: date | None = None
    strict: bool = False
    book_all: bool = True
    charges: list[Charge] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    # Kobo charged outside the harness loop and not yet folded into the run.
    unfolded_kobo: int = 0
    unfolded_calls: int = 0

    def take(self, c: Charge) -> None:
        self.charges.append(c)
        if c.booked:
            return
        if self.book_all and self.control is not None and self.day is not None:
            book(self.control, self.tenant_id, self.helper or c.role, self.day, c.kobo)
        if self.book_all:
            self.unfolded_kobo += c.kobo
            self.unfolded_calls += 1

    def fold(self) -> tuple[int, int]:
        """(kobo, calls) charged outside the loop since the last fold."""
        out = (self.unfolded_kobo, self.unfolded_calls)
        self.unfolded_kobo = self.unfolded_calls = 0
        return out

    def tally(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for c in self.charges:
            row = out.setdefault(c.role, {"calls": 0, "kobo": 0, "prompt_tokens": 0,
                                          "completion_tokens": 0, "cached_tokens": 0})
            row["calls"] += 1
            row["kobo"] += c.kobo
            row["prompt_tokens"] += c.prompt_tokens
            row["completion_tokens"] += c.completion_tokens
            row["cached_tokens"] += c.cached_tokens
        return out


_SCOPE: contextvars.ContextVar[Scope | None] = contextvars.ContextVar("mimi_meter_scope",
                                                                      default=None)
_ROLE: contextvars.ContextVar[str] = contextvars.ContextVar("mimi_meter_role", default="")
_LISTENERS: list[Callable[[Charge], None]] = []
_LOCK = threading.Lock()
violations: list[str] = []


def add_listener(fn: Callable[[Charge], None]) -> None:
    with _LOCK:
        _LISTENERS.append(fn)


def remove_listener(fn: Callable[[Charge], None]) -> None:
    with _LOCK:
        if fn in _LISTENERS:
            _LISTENERS.remove(fn)


@contextmanager
def scope(**kw: Any) -> Iterator[Scope]:
    """Open a metering scope (one turn). Nested scopes replace the outer one."""
    s = Scope(**kw)
    tok = _SCOPE.set(s)
    try:
        yield s
    finally:
        _SCOPE.reset(tok)


def current() -> Scope | None:
    return _SCOPE.get()


@contextmanager
def metered(role: str) -> Iterator[None]:
    """The inner client call of a metered wrapper (``guard`` lets it through)."""
    tok = _ROLE.set(role)
    try:
        yield
    finally:
        _ROLE.reset(tok)


def guard(client: str) -> None:
    """Called on entry by every model client. Outside ``metered`` it is a bypass."""
    if _ROLE.get():
        return
    s = _SCOPE.get()
    if s is None:
        return  # not inside an agent turn (an eval or a unit test driving a client)
    msg = f"model client {client!r} called outside the metering seam"
    s.violations.append(msg)
    with _LOCK:
        violations.append(msg)
    if s.strict:
        raise UnmeteredCall(msg)
    log.error(msg)


def charge(role: str, model: str, *, prompt_tokens: int = 0, completion_tokens: int = 0,
           cached_tokens: int = 0, kobo: int | None = None, units: float = 0.0,
           booked: bool = False) -> Charge:
    """Price one call and hand it to the scope and the listeners."""
    k = (kobo if kobo is not None
         else kobo_cost(model, prompt_tokens, completion_tokens, cached_tokens))
    c = Charge(role, model, int(k), int(prompt_tokens), int(completion_tokens),
               int(cached_tokens), float(units), booked)
    s = _SCOPE.get()
    if s is not None:
        s.take(c)
    with _LOCK:
        listeners = list(_LISTENERS)
    for fn in listeners:
        try:
            fn(c)
        except Exception:  # a report hook never breaks a turn
            log.warning("metering listener failed", exc_info=True)
    return c


# ------------------------------------------------------------------ wrappers


class _Chat(Protocol):
    name: str

    def complete(self, messages: Sequence[Any], *, max_tokens: int = 400,
                 meta: Mapping[str, str] | None = None,
                 response_format: Mapping[str, object] | None = None) -> Any: ...


class Metered:
    """A chat model whose every call is charged under ``role``."""

    def __init__(self, inner: _Chat, role: str) -> None:
        self.inner: Any = inner.inner if isinstance(inner, Metered) else inner
        self.role = role
        self.name = self.inner.name

    def complete(self, messages: Sequence[Any], *, max_tokens: int = 400,
                 meta: Mapping[str, str] | None = None,
                 response_format: Mapping[str, object] | None = None) -> Any:
        with metered(self.role):
            res = self.inner.complete(messages, max_tokens=max_tokens, meta=meta,
                                      response_format=response_format)
        charge(self.role, res.model or self.name, prompt_tokens=res.prompt_tokens,
               completion_tokens=res.completion_tokens, cached_tokens=_cached_of(res))
        return res

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)


def _cached_of(res: Any) -> int:
    usage = getattr(res, "raw_usage", None) or {}
    details = usage.get("prompt_tokens_details") if isinstance(usage, Mapping) else None
    if isinstance(details, Mapping):
        return int(details.get("cached_tokens") or 0)
    return int(getattr(res, "cached_tokens", 0) or 0)


class MeteredSegmenter:
    """SAM 3.1: charged per image segmented."""

    def __init__(self, inner: Any, role: str = SAM) -> None:
        self.inner: Any = inner.inner if isinstance(inner, MeteredSegmenter) else inner
        self.role = role
        self.name = getattr(self.inner, "name", "sam")

    def segment(self, image: bytes, mime: str, prompt: str) -> Any:
        with metered(self.role):
            seg = self.inner.segment(image, mime, prompt)
        n = int(getattr(seg, "images", 1) or 1)
        if self.name != "none":
            charge(self.role, getattr(seg, "model", self.name), kobo=sam_kobo(n), units=n)
        return seg

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)


class MeteredTranscriber:
    """Speech to text: charged per second of audio at the provider's rate."""

    def __init__(self, inner: Any, role: str = STT) -> None:
        self.inner: Any = inner.inner if isinstance(inner, MeteredTranscriber) else inner
        self.role = role
        self.name = getattr(self.inner, "name", "stt")

    def transcribe(self, clip: Any, ctx: Any) -> Any:
        with metered(self.role):
            t = self.inner.transcribe(clip, ctx)
        provider = str(getattr(t, "provider", "") or self.name)
        secs = getattr(t, "duration_s", None) or getattr(clip, "duration_s", None)
        charge(self.role, provider, kobo=audio_kobo(provider, secs), units=float(secs or 0))
        return t

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)


def is_metered(obj: Any) -> bool:
    return isinstance(obj, (Metered, MeteredSegmenter, MeteredTranscriber))
