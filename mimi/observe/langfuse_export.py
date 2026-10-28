"""Send shop turns to Langfuse, only when it is switched on.

``exporter_from_env()`` returns a ``LangfuseExporter`` only when
``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY`` are both set and the
process is not running in CI (``CI`` / ``GITHUB_ACTIONS``). Otherwise it is
``NoopExporter``: no client is built, no thread starts, no network call is
made. ``LANGFUSE_HOST`` (or ``LANGFUSE_BASE_URL``) defaults to
https://cloud.langfuse.com.

The exporter never blocks or breaks a turn:

* ``export`` puts the finished turn on a bounded queue and returns; a
  background thread builds the trace with the Langfuse Python SDK
  (OpenTelemetry-based, pinned in ``pyproject.toml``), which batches and
  sends it from its own thread;
* the SDK has a request timeout (``LANGFUSE_TIMEOUT``, default 5 s here);
* a full queue drops the turn with a warning; any exception in the worker is
  caught and logged as a warning; ``close`` waits at most ``timeout_s``.

Everything is redacted (``mimi.observe.redact``) before it is handed to the
SDK, and the SDK's ``mask`` hook runs the same redaction again. Media (the
synthetic screenshot, the synthetic voice clip) is attached as
``LangfuseMedia`` only when the turn is marked synthetic *and*
``MIMI_TRACE_MEDIA`` is not ``0``: real shop images and audio never leave.

Trace shape (one trace per shop turn), names stable and verb-first:

    handle-receipt-reply | handle-voice-note | check-payment-screenshot | ...  (agent)
      transcribe-voice-note        (generation: Spitch / Muse Voice Transcribe)
      read-payment-screenshot      (span: extracted fields + image hash [+ media])
        read-payment-screenshot-fields (generation: Muse Spark)
      judge-receipt-reply          (generation: Muse Spark)
      decide-next-action           (chain: the flow step and what it chose)
      check-<rule> ...             (tool: each code check)

Trace attributes: user_id = shop id (pseudonymous), session_id = shop + the
delivery or payment conversation, tags = feature, verdict, language,
distributor; environment and release (git sha) on the client; prompt version
in generation metadata. Scores go on the trace (``Turn.scores`` and later
``Feedback``).
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from mimi.observe.redact import redact
from mimi.observe.turns import Feedback, NoopExporter, Observation, Turn, TurnExporter

log = logging.getLogger(__name__)

DEFAULT_HOST = "https://cloud.langfuse.com"
LANGFUSE_SDK_VERSION = "5.0.4"  # keep in step with pyproject.toml [langfuse]


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def in_ci(env: Mapping[str, str]) -> bool:
    return _truthy(env.get("CI")) or _truthy(env.get("GITHUB_ACTIONS"))


def git_release() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], capture_output=True,
                             text=True, timeout=2, check=True)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


@dataclass(frozen=True)
class LangfuseSettings:
    public_key: str
    secret_key: str = field(repr=False)
    host: str = DEFAULT_HOST
    environment: str = "dev"
    release: str = "unknown"
    timeout_s: int = 5
    media: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None,
                 environment: str | None = None) -> LangfuseSettings | None:
        e = env if env is not None else os.environ
        pk = e.get("LANGFUSE_PUBLIC_KEY", "").strip()
        sk = e.get("LANGFUSE_SECRET_KEY", "").strip()
        if not pk or not sk or in_ci(e):
            return None
        return cls(
            public_key=pk,
            secret_key=sk,
            host=(e.get("LANGFUSE_HOST") or e.get("LANGFUSE_BASE_URL") or DEFAULT_HOST).strip(),
            environment=(environment or e.get("LANGFUSE_TRACING_ENVIRONMENT") or "dev").strip(),
            release=(e.get("LANGFUSE_RELEASE") or git_release()).strip(),
            timeout_s=int(e.get("LANGFUSE_TIMEOUT", "5") or 5),
            media=e.get("MIMI_TRACE_MEDIA", "1").strip() != "0",
        )


def _mask(*, data: Any, **kwargs: Any) -> Any:
    return redact(data)


def make_client(s: LangfuseSettings) -> Any:
    """A Langfuse client for these settings (imports the SDK lazily)."""
    from langfuse import Langfuse

    os.environ.setdefault("OTEL_SERVICE_NAME", "mimi-agent")
    return Langfuse(
        public_key=s.public_key,
        secret_key=s.secret_key,
        base_url=s.host,
        timeout=s.timeout_s,
        environment=s.environment,
        release=s.release,
        mask=_mask,
        flush_interval=2.0,
    )


@dataclass
class SentTrace:
    job_id: str
    trace_id: str
    name: str
    flow: str
    verdict: str
    tags: list[str]
    public: bool = False


_STOP = object()


class LangfuseExporter:
    """Background, non-blocking export of finished turns."""

    enabled = True

    def __init__(
        self,
        settings: LangfuseSettings,
        *,
        client_factory: Callable[[LangfuseSettings], Any] = make_client,
        max_queue: int = 1000,
        public_if: Callable[[Turn], bool] | None = None,
    ) -> None:
        self.settings = settings
        self._factory = client_factory
        self._client: Any = None
        self._q: queue.Queue[Any] = queue.Queue(maxsize=max_queue)
        self.public_if = public_if
        self.sent: list[SentTrace] = []
        self.scores_sent = 0
        self.errors = 0
        self.dropped = 0
        # Each process run gets its own trace ids, so a rerun never writes into
        # an earlier run's traces (ids stay stable within a run for late scores).
        self.run_id = os.environ.get("MIMI_TRACE_RUN") or uuid.uuid4().hex[:12]
        self._pending = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="mimi-langfuse", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ API

    def export(self, turn: Turn) -> None:
        self._put(("turn", turn))

    def feedback(self, fb: Feedback) -> None:
        self._put(("feedback", fb))

    def flush(self, timeout_s: float = 10.0) -> bool:
        """Wait until every queued item is sent and the SDK has flushed it (a
        worker that may be killed next calls this after each job). Never raises;
        False when it did not finish in time."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                if self._pending <= 0:
                    break
            time.sleep(0.02)
        else:
            return False
        if self._client is None:
            return True
        flusher = threading.Thread(target=self._safe_flush, daemon=True)
        flusher.start()
        flusher.join(max(0.1, deadline - time.monotonic()))
        return not flusher.is_alive()

    def close(self, timeout_s: float = 10.0) -> None:
        deadline = time.monotonic() + timeout_s
        self._put(_STOP)
        self._thread.join(max(0.1, deadline - time.monotonic()))
        if self._client is None:
            return
        flusher = threading.Thread(target=self._safe_flush, daemon=True)
        flusher.start()
        flusher.join(max(0.1, deadline - time.monotonic()))
        if flusher.is_alive():
            log.warning("tracing: Langfuse flush did not finish in %.0fs", timeout_s)

    def trace_url(self, trace_id: str) -> str:
        client = self._client
        try:
            return str(client.get_trace_url(trace_id=trace_id)) if client else ""
        except Exception:
            return ""

    # ------------------------------------------------------------ internals

    def _put(self, item: Any) -> None:
        try:
            with self._lock:
                self._pending += 1
            self._q.put_nowait(item)
        except queue.Full:
            with self._lock:
                self._pending -= 1
            self.dropped += 1
            log.warning("tracing: export queue full; dropped one item")

    def _safe_flush(self) -> None:
        try:
            self._client.flush()
        except Exception as exc:
            log.warning("tracing: Langfuse flush failed (%s)", type(exc).__name__)

    def _ensure(self) -> Any:
        if self._client is None:
            self._client = self._factory(self.settings)
        return self._client

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is _STOP:
                with self._lock:
                    self._pending -= 1
                return
            kind, obj = item
            try:
                if kind == "turn":
                    self._send(obj)
                else:
                    self._score(obj)
            except Exception as exc:  # never let tracing break anything
                self.errors += 1
                log.warning("tracing: Langfuse export failed (%s: %s)", type(exc).__name__,
                            str(exc)[:160])
            finally:
                with self._lock:
                    self._pending -= 1

    def trace_id_for(self, seed: str) -> str:
        from langfuse import Langfuse

        return str(Langfuse.create_trace_id(seed=f"{self.run_id}:{seed}"))

    def _media(self, turn: Turn, m: Any) -> Any:
        if not (turn.synthetic and self.settings.media):
            return {"sha256": m.sha256, "bytes": len(m.data), "mime": m.mime}
        from langfuse.media import LangfuseMedia

        return LangfuseMedia(content_bytes=m.data, content_type=m.mime)

    def _with_media(self, turn: Turn, value: Any, media: Mapping[str, Any]) -> Any:
        """Redact, then put media (or its hash) where its placeholder was."""
        clean = redact(value)
        if not media:
            return clean

        def swap(v: Any) -> Any:
            if isinstance(v, str) and v in media:
                return self._media(turn, media[v])
            if isinstance(v, dict):
                return {k: swap(x) for k, x in v.items()}
            if isinstance(v, list):
                return [swap(x) for x in v]
            return v

        return swap(clean)

    def _send(self, turn: Turn) -> None:
        from langfuse import propagate_attributes

        client = self._ensure()
        trace_id = self.trace_id_for(turn.trace_seed)
        tags = turn.tags
        meta = {
            "flow": turn.flow,
            "feature": turn.feature,
            "verdict": turn.verdict,
            "distributor": "coral-lane",
            "job_id": turn.job_id,
            "thread_id": turn.thread_id,
            "turn": str(turn.number),
            "synthetic": str(turn.synthetic).lower(),
            "run_id": self.run_id,
        }
        root_meta = redact({**meta, **{k: v for k, v in turn.metadata.items()}})
        with client.start_as_current_observation(
            name=turn.name,
            as_type="agent",
            trace_context={"trace_id": trace_id},
            input=self._with_media(turn, turn.input, self._all_media(turn.observations)),
            output=redact(turn.output),
            metadata=root_meta,
            end_on_exit=False,
        ) as root:
            t0 = time.time_ns()
            with propagate_attributes(
                user_id=turn.user_id,
                session_id=turn.session_id,
                tags=tags,
                trace_name=turn.name,
                metadata={k: str(v)[:190] for k, v in meta.items()},
            ):
                span_total = 0
                for obs in turn.observations:
                    span_total = max(span_total, self._emit(root, turn, obs))
                public = bool(self.public_if and self.public_if(turn))
                if public:
                    root.set_trace_as_public()
            root.end(end_time=t0 + max(span_total, turn.ended_ns - turn.started_ns, 1))
        for fb in turn.scores:
            self._score(fb)
        self.sent.append(SentTrace(turn.job_id, trace_id, turn.name, turn.flow, turn.verdict,
                                   tags, public))

    def _all_media(self, obs: list[Observation]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for o in obs:
            out.update(o.media)
            out.update(self._all_media(o.children))
        return out

    def _emit(self, parent: Any, turn: Turn, obs: Observation) -> int:
        """One observation (and its children) under ``parent``; its duration."""
        kw: dict[str, Any] = {
            "name": obs.name,
            "as_type": obs.kind,
            "input": self._with_media(turn, obs.input, obs.media),
            "output": self._with_media(turn, obs.output, obs.media),
            "metadata": redact({**obs.metadata,
                                "offset_ms": round((obs.started_ns - turn.started_ns) / 1e6, 1)}),
            "level": obs.level,
        }
        if obs.status_message:
            kw["status_message"] = redact(obs.status_message)
        if obs.kind == "generation":
            kw["model"] = obs.model
            kw["usage_details"] = dict(obs.usage)
            if obs.cost_usd is not None:
                kw["metadata"]["cost_estimate_usd"] = obs.cost_usd
        child = parent.start_observation(**kw)
        start = time.time_ns()
        longest = 0
        for sub in obs.children:
            longest = max(longest, self._emit(child, turn, sub))
        dur = max(obs.duration_ns, longest, 1)
        child.end(end_time=start + dur)
        return dur

    def _score(self, fb: Feedback) -> None:
        client = self._ensure()
        value: Any = fb.value
        if fb.data_type == "BOOLEAN":
            value = 1.0 if fb.value else 0.0
        client.create_score(
            name=fb.name,
            value=value,
            trace_id=self.trace_id_for(fb.trace_seed),
            data_type=fb.data_type,
            comment=fb.comment or None,
        )
        self.scores_sent += 1


def exporter_from_env(
    env: Mapping[str, str] | None = None,
    *,
    environment: str | None = None,
    public_if: Callable[[Turn], bool] | None = None,
) -> TurnExporter:
    """``LangfuseExporter`` when the keys are set (and not in CI), else no-op."""
    settings = LangfuseSettings.from_env(env, environment)
    if settings is None:
        return NoopExporter()
    try:
        import langfuse  # noqa: F401
    except ImportError:
        log.warning("tracing: LANGFUSE_* keys are set but the langfuse extra is not "
                    "installed (pip install -e '.[langfuse]'); tracing is off")
        return NoopExporter()
    return LangfuseExporter(settings, public_if=public_if)
