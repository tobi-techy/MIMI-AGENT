"""Model adapter: FakeModel offline, Meta client timeouts and one retry."""

from __future__ import annotations

import json

import httpx
import pytest

from mimi.models import (
    ChatMessage,
    FakeModel,
    MetaChatModel,
    ModelError,
    from_env,
)

MSG = [ChatMessage("user", "hello")]


def test_no_key_means_fake_model() -> None:
    model = from_env("mimi", env={})
    assert isinstance(model, FakeModel)
    assert model.complete(MSG).fake


def test_key_means_meta_client_with_env_models() -> None:
    env = {
        "META_API_KEY": "test-key",  # pragma: allowlist secret (fake test value)
        "SIM_MODEL": "muse-spark-1.3-contributor",
    }
    model = from_env("sim", env=env)
    assert isinstance(model, MetaChatModel)
    assert model.name == "muse-spark-1.3-contributor"
    assert from_env("mimi", env={"META_API_KEY": "k"}).name == "muse-spark-1.3"


def _ok(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert body["model"] == "muse-spark-1.3"
    assert "temperature" not in body and "stop" not in body
    assert request.headers["authorization"] == "Bearer test-key"
    assert str(request.url) == "https://api.meta.ai/v1/chat/completions"
    return httpx.Response(
        200,
        json={
            "model": "muse-spark-1.3",
            "choices": [{"message": {"content": " YES "}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        },
    )


def test_meta_client_parses_reply_and_usage() -> None:
    m = MetaChatModel(
        api_key="test-key", model="muse-spark-1.3", transport=httpx.MockTransport(_ok)
    )
    res = m.complete(MSG)
    assert (res.text, res.prompt_tokens, res.completion_tokens) == ("YES", 12, 3)


def test_meta_client_retries_once_then_succeeds() -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return _ok(request)

    m = MetaChatModel(
        api_key="test-key",  # pragma: allowlist secret (fake test value)
        model="muse-spark-1.3",
        transport=httpx.MockTransport(flaky),
        sleep=lambda s: None,
    )
    assert m.complete(MSG).text == "YES"
    assert calls["n"] == 2


def test_meta_client_gives_up_after_one_retry_without_leaking_the_key() -> None:
    calls = {"n": 0}

    def down(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"error": "busy"})

    m = MetaChatModel(
        api_key="secret-key-value",  # pragma: allowlist secret (fake test value)
        model="muse-spark-1.3",
        transport=httpx.MockTransport(down),
        sleep=lambda s: None,
    )
    with pytest.raises(ModelError) as err:
        m.complete(MSG)
    assert calls["n"] == 2
    assert "secret-key-value" not in str(err.value)


def test_meta_client_does_not_retry_a_bad_request() -> None:
    calls = {"n": 0}

    def bad(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad"})

    m = MetaChatModel(
        api_key="k", model="m", transport=httpx.MockTransport(bad), sleep=lambda s: None
    )
    with pytest.raises(ModelError):
        m.complete(MSG)
    assert calls["n"] == 1
