"""What a chat model looks like to Mimi and the simulator."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

Role = Literal["developer", "system", "user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class ChatResult:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    fake: bool = False
    # Whatever cost the API reported, verbatim; most do not report one.
    raw_usage: Mapping[str, object] = field(default_factory=dict)


class ModelError(RuntimeError):
    """The model call failed after its retry."""


class ChatModel(Protocol):
    name: str

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int = 400,
        meta: Mapping[str, str] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> ChatResult:
        """``meta`` is for the FakeModel's script and traces; never sent.

        ``response_format`` is passed to the API as is (for example a JSON
        schema); fakes ignore it.
        """
        ...
