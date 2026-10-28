"""Tracing (mimi.observe): redaction, the no-op without keys, the turn shape,
and that a broken exporter never breaks a turn. No network: the Langfuse
client is a mock, and a socket guard fails any connection attempt."""

from __future__ import annotations

import io
import json
import socket
from contextlib import contextmanager, redirect_stdout
from typing import Any

import pytest

from mimi.observe import langfuse_export as lx
from mimi.observe.redact import redact, redact_text
from mimi.observe.turns import NoopExporter, RecordingExporter, TurnBook
from mimi.sim.__main__ import main as sim_main

SCREENS = ["--shop-ids", "shop-019,shop-037,shop-025", "--screenshots", "1", "--fakes", "0.4"]
KEYS = {"LANGFUSE_PUBLIC_KEY": "pk-lf-test", "LANGFUSE_SECRET_KEY": "sk-lf-test"}  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_API_KEY", "")
    monkeypatch.setenv("SPITCH_API_KEY", "")

    def no_network(*a: Any, **k: Any) -> None:
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)


def _sim(args: list[str], exporter: Any) -> dict[str, Any]:
    out = io.StringIO()
    with redirect_stdout(out):
        assert sim_main(args, exporter=exporter) == 0
    text = out.getvalue()
    return dict(json.loads(text[text.index("{"):]))


# ----------------------------------------------------------------- redaction


def test_redaction_rules() -> None:
    raw = {
        "text": "Call +234 803 123 4567 or 08031234567. Acct 0123456789 ref 1234567890123.",
        "auth": "Authorization: Bearer abc.def.ghi and api_key=XYZ and sk-lf-1234abcd-ee",
        "image": b"\x89PNG not really",
        "url": "data:image/png;base64,aGVsbG8=",
        "recipient_account_as_printed": "0123 456 789",
        "sender_phone": "+2348031234567",
        "secret_key": "sk-anything",  # pragma: allowlist secret
        "sha": "64168d715684995cee218ccca85f7192783a5fdf8a49c833061da21fb0cb3192",  # pragma: allowlist secret
        "amount": "N84,500 on 2026-10-26 14:05",
    }
    out = redact(raw)
    flat = json.dumps(out)
    assert "+234******4567" in out["text"] and "0******4567" in out["text"]
    assert "******6789" in out["text"] and "123456789" not in flat
    assert "8031234567" not in flat
    assert "abc.def" not in flat and "XYZ" not in flat and "sk-lf-1234" not in flat
    assert out["image"] == {"sha256": out["image"]["sha256"], "bytes": 15}
    assert out["url"].startswith("[image/png sha256=") and "aGVsbG8" not in flat
    assert out["recipient_account_as_printed"] == "******6789"
    assert out["sender_phone"] == "+234******4567"
    assert out["secret_key"] == "[redacted]"
    assert out["sha"] == raw["sha"]  # hashes are kept
    assert out["amount"] == raw["amount"]  # amounts and dates are not digits runs
    assert redact_text("no secrets here") == "no secrets here"


def test_redaction_never_raises() -> None:
    class Weird:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert redact({"x": Weird()}) == {"x": "[redacted]"}


# ------------------------------------------------------------------- no-op


def test_no_keys_means_noop() -> None:
    assert isinstance(lx.exporter_from_env({}), NoopExporter)
    assert isinstance(lx.exporter_from_env({"LANGFUSE_PUBLIC_KEY": "pk"}), NoopExporter)
    assert isinstance(lx.exporter_from_env({**KEYS, "CI": "true"}), NoopExporter)
    assert isinstance(lx.exporter_from_env({**KEYS, "GITHUB_ACTIONS": "true"}), NoopExporter)
    s = lx.LangfuseSettings.from_env(KEYS)
    assert s is not None and s.host == "https://cloud.langfuse.com"
    assert "sk-lf-test" not in repr(s)


def test_sim_without_keys_builds_no_client_and_no_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)

    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("no client without keys")

    monkeypatch.setattr(lx, "make_client", boom)
    report = _sim(SCREENS, exporter=None)
    assert "traces_exported" not in report
    assert TurnBook().enabled is False


def test_tracing_does_not_change_the_runtime_spans() -> None:
    plain = _sim(SCREENS, NoopExporter())
    traced = _sim(SCREENS, RecordingExporter())
    skip = {"wall_seconds", "traces_exported"}
    assert {k: v for k, v in plain.items() if k not in skip} == {
        k: v for k, v in traced.items() if k not in skip
    }


# ------------------------------------------------------------- turn shape


def test_turns_are_one_per_shop_message_with_attributes() -> None:
    rec = RecordingExporter()
    _sim(SCREENS, rec)
    flows = {t.flow for t in rec.turns}
    assert flows == {"receipt_reply", "payment_screenshot"}
    fake = [t for t in rec.turns if t.metadata.get("sim_fake")]
    assert fake and all(t.verdict == "looks_fake" for t in fake)
    for t in rec.turns:
        assert t.user_id.startswith("shop-") and "+" not in t.user_id
        assert t.session_id.startswith(t.shop_id + ":")
        assert t.feature in t.tags and any(x.startswith("verdict:") for x in t.tags)
        assert "distributor:coral-lane" in t.tags
        names = {s.name for s in t.scores}
        assert "turn_error" in names
    shot = next(t for t in rec.turns if t.flow == "payment_screenshot" and t.verdict != "duplicate")
    read = next(o for o in shot.observations if o.name == "read-payment-screenshot")
    assert read.kind == "span" and read.children[0].kind == "generation"
    assert any(o.kind == "tool" and o.name.startswith("check-") for o in shot.observations)
    assert {s.name for s in fake[0].scores} >= {"fake_ended_likely", "fake_caught"}
    assert rec.feedbacks and {f.name for f in rec.feedbacks} == {"bank_confirmed"}


# --------------------------------------------------------- mocked exporter


class _Obs:
    def __init__(self, log: list[dict[str, Any]], kw: dict[str, Any]) -> None:
        self.log, self.kw = log, kw
        log.append(kw)

    def start_observation(self, **kw: Any) -> _Obs:
        return _Obs(self.log, kw)

    def end(self, **kw: Any) -> None:
        self.kw["ended"] = True

    def set_trace_as_public(self) -> None:
        self.kw["public"] = True


class FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.obs: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self.flushed = 0

    @contextmanager
    def start_as_current_observation(self, **kw: Any) -> Any:
        if self.fail:
            raise ConnectionError("langfuse down")
        yield _Obs(self.obs, kw)

    def create_score(self, **kw: Any) -> None:
        self.scores.append(kw)

    def flush(self) -> None:
        self.flushed += 1

    def get_trace_url(self, trace_id: str) -> str:
        return f"https://example.invalid/{trace_id}"


def _exporter(client: FakeClient, media: bool = True) -> lx.LangfuseExporter:
    pytest.importorskip("langfuse")
    s = lx.LangfuseSettings(public_key="pk-lf-test", secret_key="sk-lf-test", media=media)
    return lx.LangfuseExporter(s, client_factory=lambda _: client,
                               public_if=lambda t: t.verdict == "looks_fake")


def test_exporter_sends_redacted_traces_and_scores() -> None:
    client = FakeClient()
    ex = _exporter(client, media=False)
    _sim(SCREENS, ex)
    assert ex.errors == 0 and client.flushed >= 1
    roots = [o for o in client.obs if o.get("as_type") == "agent"]
    assert len(roots) == len(ex.sent) >= 5
    assert any(o.get("public") for o in roots)
    gens = [o for o in client.obs if o.get("as_type") == "generation"]
    assert gens and all(o["model"] and "input" in o["usage_details"] for o in gens)
    blob = json.dumps(client.obs, default=str)
    assert "+2340" not in blob and "base64," not in blob
    assert "@@@langfuseMedia" not in blob  # media off: hashes only
    assert "image_sha256" in blob
    names = {s["name"] for s in client.scores}
    assert {"turn_error", "fake_caught", "bank_confirmed", "shop_reply"} <= names
    assert all(s["trace_id"] for s in client.scores)


def test_flush_sends_each_job_before_the_next_claim() -> None:
    # A worker that may be SIGKILLed next calls flush() after every job, so a
    # finished job's trace is out before the kill (RAI-272: 37 jobs, 37 roots).
    client = FakeClient()
    ex = _exporter(client, media=False)
    _sim(SCREENS, ex)
    assert ex.flush(5.0) is True
    roots = [o for o in client.obs if o.get("as_type") == "agent"]
    assert len(roots) == len(ex.sent) and ex._pending == 0
    before = client.flushed
    assert ex.flush(5.0) is True and client.flushed == before + 1


def test_broken_langfuse_never_breaks_a_turn() -> None:
    client = FakeClient(fail=True)
    ex = _exporter(client)
    report = _sim(SCREENS, ex)
    assert ex.errors >= 1 and report["proofs_to_approvals"] >= 1


def test_factory_that_raises_is_swallowed() -> None:
    pytest.importorskip("langfuse")
    s = lx.LangfuseSettings(public_key="pk", secret_key="sk")

    def bad(_: Any) -> Any:
        raise RuntimeError("cannot build a client")

    ex = lx.LangfuseExporter(s, client_factory=bad)
    _sim(SCREENS, ex)
    assert ex.errors >= 1


def test_owner_decision_scores_the_last_turn() -> None:
    rec = RecordingExporter()
    book = TurnBook(rec)
    with book.prelude("proof-x", flow="payment_screenshot", shop_id="shop-1",
                      thread_id="wa-shop-1") as turn:
        assert turn is not None
    book.close("proof-x", "looks_fake")
    book.owner_decision("proof-x", approved=False, comment="owner: not paid")
    assert [(f.name, f.value, f.trace_seed) for f in rec.feedbacks] == [
        ("owner_approved", False, "proof-x#turn0")
    ]


def test_experiment_code_evaluators() -> None:
    pytest.importorskip("langfuse")
    from mimi.observe import experiments as ex

    fake = {"verdict": "looks_fake", "fake": True, "fake_kind": "edited_amount",
            "fields": {"amount_kobo": 8450000, "sender": "A B"}}
    out = {"verdict": "likely_genuine", "fields": {"amount": 8450000, "sender": "A B"}}
    assert ex.verdict_correct(output=out, expected_output=fake).value is False
    safety = {e.name: e.value for e in ex.proof_safety(output=out, expected_output=fake)}
    assert safety == {"fake_caught": False, "false_likely_genuine": True}
    assert ex.field_accuracy(output=out, expected_output=fake)[0].value == 1.0
    rc = ex.false_confirm(output={"action": "consent"},
                          expected_output={"verdict": "disputed", "also_ok": []})
    assert rc.value is True
    assert "{{reply}}" in ex.JUDGE_PROMPT and "{{allowed}}" in ex.JUDGE_PROMPT
