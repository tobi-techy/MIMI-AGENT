"""One span per step, shaped for Langfuse.

Langfuse ingests OpenTelemetry over OTLP, so the runtime emits plain OTel spans
with the ``langfuse.*`` attributes it understands: a job is a trace (session =
thread), each step an observation. Point ``OTEL_ENDPOINT`` at a collector that
forwards to Langfuse (see ``otel-config.yaml``); with tracing off the spans are
no-ops. Tests use ``RecordingStepTracer``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Protocol

AttrValue = str | int | float | bool


class StepSpan(Protocol):
    def set(self, key: str, value: AttrValue) -> None: ...


class StepTracer(Protocol):
    def step(
        self, *, job_id: str, thread_id: str, helper: str, step_index: int
    ) -> AbstractContextManager[StepSpan]: ...


@dataclass
class RecordedSpan:
    name: str
    attrs: dict[str, AttrValue] = field(default_factory=dict)
    error: str | None = None

    def set(self, key: str, value: AttrValue) -> None:
        self.attrs[key] = value


class RecordingStepTracer:
    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []

    @contextmanager
    def step(
        self, *, job_id: str, thread_id: str, helper: str, step_index: int
    ) -> Iterator[StepSpan]:
        span = RecordedSpan(f"mimi.step {helper}#{step_index}")
        span.attrs.update(
            {"job_id": job_id, "thread_id": thread_id, "step_index": step_index}
        )
        self.spans.append(span)
        try:
            yield span
        except BaseException as exc:
            span.error = type(exc).__name__
            raise


class _OtelSpan:
    def __init__(self, span: object) -> None:
        self._span = span

    def set(self, key: str, value: AttrValue) -> None:
        setter = getattr(self._span, "set_attribute", None)
        if setter is not None:
            setter(f"mimi.{key}", value)


class OtelStepTracer:
    """OpenTelemetry spans carrying Langfuse's attribute names."""

    def __init__(self, service: str = "mimi-runtime") -> None:
        from opentelemetry import trace

        self._tracer = trace.get_tracer(service)

    @contextmanager
    def step(
        self, *, job_id: str, thread_id: str, helper: str, step_index: int
    ) -> Iterator[StepSpan]:
        with self._tracer.start_as_current_span(
            f"mimi.step {helper}#{step_index}"
        ) as span:
            span.set_attribute("langfuse.trace.name", f"job {job_id}")
            span.set_attribute("langfuse.session.id", thread_id)
            span.set_attribute("langfuse.observation.type", "span")
            span.set_attribute("langfuse.trace.metadata.helper", helper)
            span.set_attribute("mimi.job_id", job_id)
            span.set_attribute("mimi.step_index", step_index)
            yield _OtelSpan(span)
