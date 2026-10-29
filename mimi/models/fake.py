"""A deterministic stand-in for the model, used whenever no key is set.

``FakeModel(responder)`` answers with ``responder(messages, meta)``; the
default responder returns a fixed acknowledgement. The same input always gives
the same output, so CI is offline and repeatable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from mimi.models import metering
from mimi.models.base import ChatMessage, ChatResult

Responder = Callable[[Sequence[ChatMessage], Mapping[str, str]], str]


def _default(messages: Sequence[ChatMessage], meta: Mapping[str, str]) -> str:
    return "ok"


class FakeModel:
    def __init__(self, responder: Responder = _default, name: str = "fake") -> None:
        self.name = name
        self._responder = responder
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
        self.calls += 1
        text = self._responder(messages, meta or {})
        return ChatResult(text=text, model=self.name, fake=True)
