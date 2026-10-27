"""What a chat model looks like to Mimi and the simulator."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

Role = Literal["developer", "system", "user", "assistant"]


@dataclass(frozen=True)
class ImageInput:
    """An image sent with a message (a shop's payment screenshot).

    The bytes go to the API as a base64 data URL and nowhere else: caches and
    traces key on ``sha256``, never the pixels.
    """

    data: bytes
    mime: str = "image/jpeg"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def data_url(self) -> str:
        return f"data:{self.mime};base64," + base64.b64encode(self.data).decode("ascii")


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str
    # Vision input: images shown with this message's text (user turns only).
    images: tuple[ImageInput, ...] = ()


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
