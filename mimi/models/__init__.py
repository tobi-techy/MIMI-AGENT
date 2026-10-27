"""Model adapter: Meta's Model API (OpenAI-compatible) or a deterministic fake."""

from mimi.models.base import (
    ChatMessage,
    ChatModel,
    ChatResult,
    ImageInput,
    ModelError,
)
from mimi.models.cache import CachedModel, CacheMiss
from mimi.models.env import DEFAULT_MODELS, from_env, has_key
from mimi.models.fake import FakeModel
from mimi.models.meta import MetaChatModel

__all__ = [
    "CacheMiss",
    "CachedModel",
    "DEFAULT_MODELS",
    "ChatMessage",
    "ChatModel",
    "ChatResult",
    "FakeModel",
    "ImageInput",
    "MetaChatModel",
    "ModelError",
    "from_env",
    "has_key",
]
