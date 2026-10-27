"""Shop turns: one trace per thing a shop sent, built on the step tracer.

A *turn* is one shop message Mimi had to understand and answer: a typed
receipt reply, a voice note, a payment screenshot, or the answer to a
payment-promise read-back. Its pieces happen in two places, so they are
collected here and exported once the turn is complete:

* at the front door (``ReceiptDesk`` / ``ProofDesk``) before any job runs:
  the voice-note transcription, the screenshot read. The desk wraps that work
  in ``TurnBook.prelude(job_id, ...)``;
* in the job's step, run by ``JobRunner`` inside ``tracer.step(...)``: the
  verdict model call, the code checks and the decision.

``TurnStepTracer`` is a ``StepTracer`` (``mimi.runtime.tracing``): it wraps
the tracer the runner already has (recording or OTel), so the existing spans
do not change. While a step runs it makes the job's turn current;
``TracedModel`` (a ``ChatModel`` wrapper) records every model call into the
current turn with its model, token usage, latency and a cost estimate. When
the step's attributes show a verdict, the turn is closed and handed to the
exporter. The whole turn is known by then, so its tags (feature, verdict,
language) are set when the trace is created, as Langfuse requires.

Later signals about a turn (the bank confirming a "likely" payment, the
owner approving or rejecting an approval) arrive as ``Feedback`` and are
sent as scores on the same trace: trace ids are derived from the job id and
the turn number, so no lookup is needed.

Exporters: ``NoopExporter`` (the default: nothing is built, nothing leaves
the process), ``RecordingExporter`` (tests) and ``LangfuseExporter``
(``mimi.observe.langfuse_export``). Nothing here ever raises into a turn: a
broken recorder or exporter is logged and skipped.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from mimi.models.base import ChatMessage, ChatModel, ChatResult
from mimi.runtime.tracing import AttrValue, StepSpan, StepTracer

log = logging.getLogger(__name__)

Flow = Literal["receipt_reply", "voice_note", "payment_screenshot", "promise_reply"]
ObsKind = Literal["generation", "span", "tool", "evaluator", "agent", "chain", "retriever"]
ScoreType = Literal["BOOLEAN", "CATEGORICAL", "NUMERIC"]

# Business tag per flow (Langfuse ``feature`` dimension).
FEATURE: dict[str, str] = {
    "receipt_reply": "receipt",
    "voice_note": "voice",
    "payment_screenshot": "proof",
    "promise_reply": "promise",
}
DISTRIBUTOR_TAG = "distributor:coral-lane"

# USD per 1M tokens (input, output), Meta Model API standard and contributor
# mode, from https://dev.meta.ai/docs/pricing-rate-limits (checked 27 Oct 2026).
TOKEN_PRICES: dict[str, tuple[float, float]] = {
    "muse-spark-1.3": (1.25, 4.25),
    "muse-spark-1.2": (1.25, 4.25),
    "muse-spark-1.1": (1.25, 4.25),
    "muse-spark-1.3-contributor": (0.10, 0.20),
    "muse-spark-1.2-contributor": (0.10, 0.20),
}
# USD per second of audio (docs/VOICE.md, checked 26 Oct 2026).
AUDIO_PRICES: dict[str, float] = {"spitch": 0.00042, "muse": 0.18 / 3600}


def token_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """Estimated USD for one call; ``None`` when the model is unpriced (fakes)."""
    price = TOKEN_PRICES.get(model)
    if price is None:
        return None
    return round((prompt_tokens * price[0] + completion_tokens * price[1]) / 1e6, 8)


def audio_cost(provider: str, seconds: float | None) -> float | None:
    key = next((k for k in AUDIO_PRICES if provider.startswith(k)), None)
    if key is None or not seconds:
        return None
    return round(seconds * AUDIO_PRICES[key], 8)


def prompt_version(messages: Sequence[ChatMessage], meta: Mapping[str, str] | None = None) -> str:
    """The prompt's version: the caller's name for it when given (``meta``),
    else a hash of the system (developer) prompt, so a change shows up."""
    named = (meta or {}).get("prompt_version", "")
    if named:
        return named
    system = next((m.content for m in messages if m.role in ("developer", "system")), "")
    if not system:
        return ""
    script = (meta or {}).get("script", "prompt")
    return f"{script}-" + hashlib.sha256(system.encode()).hexdigest()[:8]


def sniff_mime(data: bytes, declared: str = "") -> str:
    """The real type of synthetic bytes (a PNG is often declared as JPEG)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    return (declared or "application/octet-stream").split(";")[0].strip()


@dataclass
class Media:
    """Synthetic bytes that may be attached to a trace (``LangfuseMedia``)."""

    data: bytes
    mime: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class Observation:
    kind: ObsKind
    name: str
    started_ns: int
    duration_ns: int = 0
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: float | None = None
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    level: Literal["DEFAULT", "WARNING", "ERROR"] = "DEFAULT"
    status_message: str = ""
    # Attached only when the book is synthetic and media upload is on.
    media: dict[str, Media] = field(default_factory=dict)
    # Nested observations (a model call inside the step that made it).
    children: list[Observation] = field(default_factory=list)


@dataclass(frozen=True)
class Feedback:
    """A score on a turn's trace (Langfuse ``create_score``)."""

    name: str
    value: bool | str | float
    data_type: ScoreType
    comment: str = ""
    trace_seed: str = ""  # which turn's trace; set by the book


@dataclass
class Turn:
    job_id: str
    flow: Flow
    shop_id: str
    thread_id: str
    started_ns: int
    number: int = 0
    observations: list[Observation] = field(default_factory=list)
    verdict: str = ""
    language: str = ""
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    scores: list[Feedback] = field(default_factory=list)
    synthetic: bool = False
    ended_ns: int = 0

    @property
    def trace_seed(self) -> str:
        """Stable seed for this turn's trace id (job id + turn number)."""
        return f"{self.job_id}#turn{self.number}"

    @property
    def user_id(self) -> str:
        """The shop's pseudonymous id, never its phone number."""
        return self.shop_id

    @property
    def session_id(self) -> str:
        """One session per delivery or payment conversation."""
        return f"{self.shop_id}:{self.job_id}"

    @property
    def feature(self) -> str:
        return FEATURE[self.flow]

    @property
    def tags(self) -> list[str]:
        tags = [self.feature, f"verdict:{self.verdict or 'none'}", DISTRIBUTOR_TAG]
        if self.language:
            tags.append(f"language:{self.language}")
        return tags

    @property
    def name(self) -> str:
        return {
            "receipt_reply": "handle-receipt-reply",
            "voice_note": "handle-voice-note",
            "payment_screenshot": "check-payment-screenshot",
            "promise_reply": "handle-promise-reply",
        }[self.flow]

    def add(self, obs: Observation) -> None:
        self.observations.append(obs)

    def score(self, name: str, value: bool | str | float, data_type: ScoreType,
              comment: str = "") -> None:
        self.scores.append(Feedback(name, value, data_type, comment, self.trace_seed))


class TurnExporter(Protocol):
    enabled: bool

    def export(self, turn: Turn) -> None: ...

    def feedback(self, fb: Feedback) -> None: ...

    def close(self, timeout_s: float = 10.0) -> None: ...


class NoopExporter:
    """The default: nothing is built for export and nothing leaves."""

    enabled = False

    def export(self, turn: Turn) -> None:
        return None

    def feedback(self, fb: Feedback) -> None:
        return None

    def close(self, timeout_s: float = 10.0) -> None:
        return None


class RecordingExporter:
    enabled = True

    def __init__(self) -> None:
        self.turns: list[Turn] = []
        self.feedbacks: list[Feedback] = []

    def export(self, turn: Turn) -> None:
        self.turns.append(turn)

    def feedback(self, fb: Feedback) -> None:
        self.feedbacks.append(fb)

    def close(self, timeout_s: float = 10.0) -> None:
        return None


_current: contextvars.ContextVar[Turn | None] = contextvars.ContextVar(
    "mimi_current_turn", default=None
)


def current_turn() -> Turn | None:
    return _current.get()


@contextmanager
def nested(obs: Observation) -> Iterator[Observation]:
    """Observations noted inside the block become ``obs``'s children; ``obs``
    is added to the current turn when the block ends (no-op outside a turn)."""
    turn = _current.get()
    if turn is None:
        yield obs
        return
    before = len(turn.observations)
    try:
        yield obs
    finally:
        obs.children.extend(turn.observations[before:])
        del turn.observations[before:]
        turn.add(obs)


def note(obs: Observation) -> None:
    """Add an observation to the current turn (no-op outside a turn)."""
    turn = _current.get()
    if turn is not None:
        try:
            turn.add(obs)
        except Exception:  # pragma: no cover - defensive
            log.warning("tracing: could not record %s", obs.name)


class TurnBook:
    """Open turns by job id; closes and exports them.

    ``synthetic`` marks every turn as synthetic data (the sim and the evals),
    which is the only case in which media may be attached."""

    def __init__(self, exporter: TurnExporter | None = None, *, synthetic: bool = False) -> None:
        self.exporter: TurnExporter = exporter or NoopExporter()
        self.synthetic = synthetic
        self._open: dict[str, Turn] = {}
        self._count: dict[str, int] = {}
        self._last_seed: dict[str, str] = {}
        self._annotations: dict[str, dict[str, Any]] = {}
        self.exported = 0

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.exporter, "enabled", False))

    def turn_for(self, job_id: str, *, flow: Flow, shop_id: str, thread_id: str) -> Turn:
        turn = self._open.get(job_id)
        if turn is None:
            n = self._count.get(job_id, 0)
            turn = Turn(job_id, flow, shop_id, thread_id, time.time_ns(), number=n,
                        synthetic=self.synthetic)
            turn.metadata.update(self._annotations.get(job_id, {}))
            self._open[job_id] = turn
        return turn

    def annotate(self, job_id: str, **meta: Any) -> None:
        """Facts about a job known outside the flow (sim ground truth)."""
        if not self.enabled:
            return
        self._annotations.setdefault(job_id, {}).update(meta)
        if job_id in self._open:
            self._open[job_id].metadata.update(meta)

    @contextmanager
    def prelude(
        self, job_id: str, *, flow: Flow, shop_id: str, thread_id: str
    ) -> Iterator[Turn | None]:
        """Front-door work for a turn (transcription, screenshot read)."""
        if not self.enabled:
            yield None
            return
        turn = self.turn_for(job_id, flow=flow, shop_id=shop_id, thread_id=thread_id)
        if flow == "voice_note":
            turn.flow = "voice_note"
        token = _current.set(turn)
        try:
            yield turn
        finally:
            _current.reset(token)

    def close(self, job_id: str, verdict: str) -> Turn | None:
        turn = self._open.pop(job_id, None)
        if turn is None:
            return None
        turn.verdict = verdict
        turn.ended_ns = time.time_ns()
        self._count[job_id] = turn.number + 1
        self._last_seed[job_id] = turn.trace_seed
        try:
            self.exporter.export(turn)
            self.exported += 1
        except Exception as exc:  # tracing never breaks a turn
            log.warning("tracing: export failed (%s); turn %s dropped",
                        type(exc).__name__, job_id)
        return turn

    def discard(self, job_id: str) -> None:
        self._open.pop(job_id, None)

    def feedback(self, job_id: str, name: str, value: bool | str | float,
                 data_type: ScoreType, comment: str = "") -> None:
        """A later signal about the job's most recent exported turn."""
        if not self.enabled:
            return
        seed = self._last_seed.get(job_id)
        if seed is None:
            return
        try:
            self.exporter.feedback(Feedback(name, value, data_type, comment, seed))
        except Exception as exc:
            log.warning("tracing: feedback failed (%s)", type(exc).__name__)

    def owner_decision(self, job_id: str, approved: bool, comment: str = "") -> None:
        """The owner approved or rejected what Mimi put in their approvals."""
        self.feedback(job_id, "owner_approved", approved, "BOOLEAN", comment)

    def close_all(self, verdict: str = "no_verdict") -> None:
        """End of a run: export what never reached a verdict."""
        for job_id in list(self._open):
            if self._open[job_id].observations:
                self.close(job_id, verdict)
            else:
                self.discard(job_id)


def _walk(obs: Sequence[Observation]) -> Iterator[Observation]:
    for o in obs:
        yield o
        yield from _walk(o.children)


def _shop_texts(user_message: str) -> Any:
    """The shop's new messages from the verdict prompt's ``<shop_data>`` block."""
    import json

    try:
        raw = user_message.split("<shop_data>", 1)[1].split("</shop_data>", 1)[0]
        data = json.loads(raw)
        return [m.get("text", m) if isinstance(m, dict) else m
                for m in data.get("new_messages", [])]
    except Exception:
        return user_message[-400:]


class _TeeSpan:
    def __init__(self, inner: StepSpan) -> None:
        self.inner = inner
        self.attrs: dict[str, AttrValue] = {}

    def set(self, key: str, value: AttrValue) -> None:
        self.attrs[key] = value
        self.inner.set(key, value)


PROOF_HELPER_NAME = "payment_proof"
_CLARIFY_STAGES = frozenset({"clarified", "asked_to_type"})


def _shop_of(thread_id: str) -> str:
    return thread_id.removeprefix("wa-")


class TurnStepTracer:
    """A ``StepTracer`` that also assembles and exports shop turns.

    Wraps the runner's existing tracer; with a disabled book it only
    delegates (no turn objects, no context switch)."""

    def __init__(self, inner: StepTracer, book: TurnBook) -> None:
        self.inner = inner
        self.book = book

    @contextmanager
    def step(
        self, *, job_id: str, thread_id: str, helper: str, step_index: int
    ) -> Iterator[StepSpan]:
        cm: AbstractContextManager[StepSpan] = self.inner.step(
            job_id=job_id, thread_id=thread_id, helper=helper, step_index=step_index
        )
        if not self.book.enabled:
            with cm as span:
                yield span
            return
        flow: Flow = "payment_screenshot" if helper == PROOF_HELPER_NAME else "receipt_reply"
        turn = self.book.turn_for(
            job_id, flow=flow, shop_id=_shop_of(thread_id), thread_id=thread_id
        )
        token = _current.set(turn)
        t0 = time.monotonic_ns()
        started = time.time_ns()
        tee: _TeeSpan | None = None
        failed = False
        try:
            with cm as span:
                tee = _TeeSpan(span)
                yield tee
        except BaseException:
            failed = True
            raise
        finally:
            _current.reset(token)
            try:
                self._after(turn, tee, helper, step_index, started,
                            time.monotonic_ns() - t0, failed)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("tracing: step bookkeeping failed (%s)", type(exc).__name__)

    # ------------------------------------------------------------ internals

    def _after(self, turn: Turn, tee: _TeeSpan | None, helper: str, step_index: int,
               started: int, duration: int, failed: bool) -> None:
        attrs = dict(tee.attrs) if tee is not None else {}
        if helper == PROOF_HELPER_NAME and "bank_credit" in attrs and "verdict" not in attrs:
            self._bank(turn, attrs)
            return
        verdict = self._verdict(attrs)
        if verdict is None and not ("error" in attrs or failed):
            # Not a turn step (receipt sent, quiet hour, burst wait): keep any
            # front-door work for the step that judges it.
            if not turn.observations:
                self.book.discard(turn.job_id)
            return
        if verdict is None:
            verdict = "error"
        if attrs.get("result") == "duplicate":
            verdict = "duplicate"
        if "voice_notes" in attrs and turn.flow == "receipt_reply":
            turn.flow = "voice_note"
        if "promise_action" in attrs:
            turn.flow = "promise_reply"
        turn.language = str(attrs.get("language", "") or "")
        stage = str(attrs.get("stage", ""))
        turn.add(self._decision(helper, step_index, started, duration, attrs, verdict))
        for obs in self._checks(attrs, started + duration):
            turn.add(obs)
        turn.input = turn.input or self._input(turn)
        turn.output = self._output(turn, attrs, verdict)
        turn.metadata.update({"helper": helper, "step_index": step_index,
                              "action": attrs.get("action", attrs.get("result", "")),
                              "stage": stage})
        self._scores(turn, attrs, verdict, stage, failed or "error" in attrs)
        self.book.close(turn.job_id, verdict)

    @staticmethod
    def _verdict(attrs: Mapping[str, AttrValue]) -> str | None:
        if "verdict" in attrs:  # proof flow: likely_genuine / looks_fake / cannot_tell
            return str(attrs["verdict"])
        if "confidence" in attrs:  # receipt flow: the model's reading
            return str(attrs["outcome"])
        if attrs.get("action") == "no_transcript":
            return "no_transcript"
        if "promise_action" in attrs:
            return f"promise_{attrs['promise_action']}"
        return None

    @staticmethod
    def _decision(helper: str, step_index: int, started: int, duration: int,
                  attrs: Mapping[str, AttrValue], verdict: str) -> Observation:
        keep = {k: v for k, v in attrs.items() if k not in ("checks",)}
        return Observation(
            "chain",
            "decide-next-action",
            started,
            duration,
            input={k: attrs[k] for k in ("replies", "voice_notes", "transcripts", "read_via",
                                         "tool") if k in attrs},
            output={"verdict": verdict,
                    **{k: attrs[k] for k in ("action", "confidence", "language", "match",
                                             "held_back", "evidence_found", "stage",
                                             "promise_action", "promise_reason", "intents",
                                             "outcome") if k in attrs}},
            metadata={**keep, "helper": helper, "step_index": step_index},
            level="ERROR" if "error" in attrs else "DEFAULT",
            status_message=str(attrs.get("error", "")),
        )

    @staticmethod
    def _checks(attrs: Mapping[str, AttrValue], at: int) -> list[Observation]:
        out: list[Observation] = []
        raw = attrs.get("checks")
        if isinstance(raw, str) and raw:
            for part in raw.split(","):
                name, _, result = part.partition(":")
                out.append(
                    Observation(
                        "tool",
                        f"check-{name.replace('_', '-')}",
                        at,
                        input={"rule": name},
                        output={"result": result},
                        level="WARNING" if result == "fail" else "DEFAULT",
                    )
                )
            if "match" in attrs:
                out.append(Observation("tool", "match-open-invoice", at,
                                       input={"rule": "invoice_match"},
                                       output={"match": attrs["match"]}))
        elif "confidence" in attrs or "promise_action" in attrs:
            out.append(
                Observation(
                    "tool",
                    "check-reply-guards",
                    at,
                    input={"rules": ["evidence_quote_in_reply", "confidence_floor",
                                     "promise_readback"]},
                    output={k: attrs[k] for k in ("action", "evidence_found", "held_back",
                                                  "promise_action", "promise_reason")
                            if k in attrs},
                )
            )
        return out

    @staticmethod
    def _input(turn: Turn) -> Any:
        """What the shop sent: the screenshot or voice note (with its reading),
        or the typed messages Mimi judged."""
        for obs in turn.observations:
            if obs.name == "read-payment-screenshot" and isinstance(obs.input, dict):
                return {"shop_message": "payment screenshot", "image": obs.input.get("image"),
                        "caption": obs.input.get("caption", "")}
            if obs.name == "transcribe-voice-note" and isinstance(obs.input, dict):
                out = obs.output if isinstance(obs.output, dict) else {}
                return {"shop_message": "voice note", "audio": obs.input.get("audio"),
                        "transcript": out.get("text", "")}
        for obs in _walk(turn.observations):
            if obs.kind == "generation" and isinstance(obs.input, list):
                for m in reversed(obs.input):
                    text = m.get("content") if m.get("role") == "user" else None
                    if isinstance(text, str) and "<shop_data>" in text:
                        return {"shop_messages": _shop_texts(text)}
        return None

    @staticmethod
    def _output(turn: Turn, attrs: Mapping[str, AttrValue], verdict: str) -> Any:
        out: dict[str, Any] = {
            "verdict": verdict,
            "action": attrs.get("action", attrs.get("result", "")),
            "intents": turn.metadata.pop("proposed_intents", []),
        }
        replies = turn.metadata.pop("reply_to_shop", [])
        if replies:
            out["reply_to_shop"] = replies[0] if len(replies) == 1 else replies
        return out

    @staticmethod
    def _scores(turn: Turn, attrs: Mapping[str, AttrValue], verdict: str, stage: str,
                errored: bool) -> None:
        turn.score("turn_error", errored, "BOOLEAN")
        if turn.flow in ("receipt_reply", "voice_note"):
            turn.score("shop_reply", verdict, "CATEGORICAL",
                       "the shop's own answer to the receipt, as Mimi read it")
            disputed = verdict in ("disputed", "partial")
            turn.score("dispute_ended_in_consent", disputed and attrs.get("action") == "consent",
                       "BOOLEAN", "critical: a disputed reply must never be recorded as consent")
            turn.score("clarification_asked", stage in _CLARIFY_STAGES, "BOOLEAN")
        elif turn.flow == "payment_screenshot":
            turn.score("clarification_asked", attrs.get("result") == "asked", "BOOLEAN")
            if "sim_fake" in turn.metadata:
                fake = bool(turn.metadata["sim_fake"])
                turn.score("fake_ended_likely", fake and verdict == "likely_genuine", "BOOLEAN",
                           "critical: a fake screenshot must never end likely_genuine")
                if fake:
                    turn.score("fake_caught", verdict == "looks_fake", "BOOLEAN")
        elif turn.flow == "promise_reply":
            turn.score("promise_recorded", attrs.get("promise_action") == "record", "BOOLEAN")

    def _bank(self, turn: Turn, attrs: Mapping[str, AttrValue]) -> None:
        """The bank feed checked a "likely" payment: ground truth for the read."""
        self.book.discard(turn.job_id)
        credit = str(attrs.get("bank_credit", ""))
        if credit == "none_yet":
            return
        confirmed = credit not in ("", "none_by_deadline")
        self.book.feedback(turn.job_id, "bank_confirmed", confirmed, "BOOLEAN",
                           "bank feed credit matched" if confirmed
                           else "no credit within the bank window")


def observe_step(fn: Any) -> Any:
    """Wrap a step function so the current turn sees what it proposed to say
    to the shop (the ``text`` of each intent that has one). Pass-through
    outside a turn."""

    def step(ctx: Any) -> Any:
        out = fn(ctx)
        turn = _current.get()
        if turn is not None:
            try:
                texts = [str(t) for t in (getattr(i, "text", None) for i in out.intents) if t]
                kinds = [type(i).__name__ for i in out.intents]
                turn.metadata["proposed_intents"] = kinds
                if texts:
                    turn.metadata["reply_to_shop"] = texts
            except Exception:  # pragma: no cover - defensive
                log.warning("tracing: could not read the step's intents")
        return out

    return step


def _message_view(messages: Sequence[ChatMessage]) -> tuple[list[dict[str, Any]], dict[str, Media]]:
    """OpenAI-format messages; images become placeholders keyed by sha256."""
    out: list[dict[str, Any]] = []
    media: dict[str, Media] = {}
    for m in messages:
        if not m.images:
            out.append({"role": m.role, "content": m.content})
            continue
        parts: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
        for im in m.images:
            key = f"image:{im.sha256}"
            media[key] = Media(im.data, im.mime)
            parts.append({"type": "image_url", "image_url": {"url": key}})
        out.append({"role": m.role, "content": parts})
    return out, media


class TracedModel:
    """A ``ChatModel`` that records each call into the current turn.

    Outside a turn (or with tracing off) it is a pass-through. Images are
    recorded as their hash; the bytes are kept only for synthetic media."""

    def __init__(self, inner: ChatModel) -> None:
        self.inner = inner
        self.name = inner.name

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int = 400,
        meta: Mapping[str, str] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> ChatResult:
        turn = _current.get()
        cached_before = getattr(self.inner, "cached_calls", None)
        started = time.time_ns()
        t0 = time.monotonic_ns()
        try:
            res = self.inner.complete(
                messages, max_tokens=max_tokens, meta=meta, response_format=response_format
            )
        except Exception as exc:
            if turn is not None:
                note(Observation("generation", self._purpose(meta), started,
                                 time.monotonic_ns() - t0, model=self.name,
                                 metadata={"error": type(exc).__name__}, level="ERROR",
                                 status_message=type(exc).__name__))
            raise
        if turn is not None:
            try:
                self._record(turn, res, messages, meta, max_tokens, started, t0, cached_before,
                             response_format)
            except Exception:  # pragma: no cover - defensive
                log.warning("tracing: could not record a model call")
        return res

    def _record(self, turn: Turn, res: ChatResult, messages: Sequence[ChatMessage],
                meta: Mapping[str, str] | None, max_tokens: int, started: int, t0: int,
                cached_before: int | None,
                response_format: Mapping[str, object] | None = None) -> None:
        wall = time.monotonic_ns() - t0
        latency_ns = int(res.latency_s * 1e9) if res.latency_s else wall
        model = res.model or self.name
        usage = {"input": res.prompt_tokens, "output": res.completion_tokens}
        details = res.raw_usage.get("completion_tokens_details") if res.raw_usage else None
        reasoning = 0
        if isinstance(details, Mapping) and details.get("reasoning_tokens"):
            # Already inside completion_tokens: kept as metadata, not a usage key,
            # so Langfuse does not count it twice.
            reasoning = int(details["reasoning_tokens"])  # type: ignore[call-overload]
        view, media = _message_view(messages)
        replayed = cached_before is not None and getattr(self.inner, "cached_calls", 0) > cached_before
        note(
            Observation(
                "generation",
                self._purpose(meta),
                started,
                latency_ns,
                model=model,
                usage=usage,
                cost_usd=token_cost(model, res.prompt_tokens, res.completion_tokens),
                input=view,
                output=res.text,
                metadata={
                    "latency_ms": round(latency_ns / 1e6, 1),
                    "replayed_from_cache": replayed,
                    "fake_model": res.fake,
                    "max_tokens": max_tokens,
                    "repair_retry": (meta or {}).get("repair") == "1",
                    "prompt_version": prompt_version(messages, meta),
                    "response_format": "json_schema" if response_format else "text",
                    "reasoning_tokens": reasoning,
                },
                media=media,
            )
        )

    @staticmethod
    def _purpose(meta: Mapping[str, str] | None) -> str:
        script = (meta or {}).get("script", "")
        return {"verdict": "judge-receipt-reply", "proof_read": "read-payment-screenshot-fields",
                }.get(script, f"run-{script}" if script else "call-model")
