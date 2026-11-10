"""Every sink is redacted, and a sink that bypasses the guard fails here.

``mimi.observe.redact`` has the rules; ``mimi.observe.sinks`` is the one door
data leaves by. These tests drive that door with a hostile turn (a phone, an
account number, an API key and private model text), break the redactor on
purpose to prove the door fails closed, and scan the package so a new sink or
a direct ``.export()`` call cannot slip past review.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mimi.observe import correlation, redact, sinks
from mimi.observe.turns import Feedback, Observation, RecordingExporter, Turn, TurnBook

ROOT = Path(__file__).resolve().parents[1] / "mimi"
PHONE = "+234 803 123 4567"
ACCOUNT = "0123456789012"
KEY = "sk-live-abcdefghijklmnop"  # pragma: allowlist secret
PRIVATE = "the model thought the shop would pay after the harvest"


def hostile_turn() -> Turn:
    return Turn(
        job_id="job-1", flow="receipt_reply", shop_id="shop-001", thread_id="th-1",
        started_ns=0,
        input={"text": f"Call {PHONE} or pay {ACCOUNT}. Authorization: Bearer {KEY}",
               "sender_phone": PHONE},
        output=PRIVATE,
        metadata={"sender": "Mama Sola", "phone": PHONE, "private_note": PRIVATE},
        observations=[Observation(kind="span", name="read-payment", started_ns=0,
                                  input=f"acct {ACCOUNT}", output=PRIVATE,
                                  metadata={"msisdn": PHONE})],
    )


def flatten(turn: Turn) -> str:
    return json.dumps(dataclasses.asdict(turn), default=str)


def test_an_exported_turn_carries_no_phone_account_key_or_model_text() -> None:
    rec = RecordingExporter()
    sinks.export(rec, hostile_turn())
    assert rec.turns, "a redactable turn is sent, not dropped"
    blob = flatten(rec.turns[0])
    for secret in ("8031234567", ACCOUNT, KEY):
        assert secret not in blob, secret
    assert "******4567" in blob  # the last four stay, as the rules say
    assert redact.find_leaks(dataclasses.asdict(rec.turns[0])) == []


def test_a_broken_redactor_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """If redaction ever stops working, nothing leaks: the sink gets nothing."""
    rec = RecordingExporter()
    monkeypatch.setattr(redact, "redact", lambda value, *_a, **_k: value)
    monkeypatch.setenv("MIMI_TRACE_STRICT", "1")
    with pytest.raises(sinks.LeakError) as err:
        sinks.export(rec, hostile_turn())
    assert "phone" in str(err.value) or "long digits" in str(err.value)
    assert str(err.value).find("8031234567") == -1  # the report never carries the leak
    assert rec.turns == []

    monkeypatch.setenv("MIMI_TRACE_STRICT", "0")
    monkeypatch.setenv("CI", "")
    before = len(sinks.DROPPED)
    sinks.export(rec, hostile_turn())
    assert rec.turns == [] and len(sinks.DROPPED) == before + 1


def test_a_score_comment_is_redacted_and_checked() -> None:
    rec = RecordingExporter()
    sinks.feedback(rec, Feedback("bank_confirmed", True, "BOOLEAN",
                                 f"the shop {PHONE} said paid"))
    assert rec.feedbacks and "8031234567" not in rec.feedbacks[0].comment


def test_the_log_sink_redacts_and_tags_the_trace(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="mimi.observe.sinks"):
        with correlation.bind("turn_0123456789abcdef0123", tenant="coral-lane", attempt=2):
            sinks.log_event("turn.step", {"phone": PHONE, "text": f"acct {ACCOUNT}",
                                          "note": "Mama Sola called"})
    line = caplog.text
    assert "8031234567" not in line and ACCOUNT not in line
    assert "******4567" in line
    # The correlation id survives redaction digit for digit: it is the one
    # thing a log search keys on, and it names a trace, not a person.
    assert "turn_0123456789abcdef0123" in line and '"attempt": 2' in line
    assert redact.redact_text("turn_0123456789abcdef0123") == "turn_0123456789abcdef0123"


def test_model_reasoning_reaches_a_trace_only_as_a_count() -> None:
    """Private model text is never a trace field: the trace carries its length."""
    from mimi.harness.tracing import run_observations
    from mimi.harness.types import Event

    event = Event(event_id="e1", run_id="r1", thread_id="th", job_id="j1", seq=0, step=0,
                  type="step_started", at=datetime(2026, 10, 26, tzinfo=UTC))
    called = Event(event_id="e2", run_id="r1", thread_id="th", job_id="j1", seq=1, step=0,
                   type="model_called", at=datetime(2026, 10, 26, tzinfo=UTC),
                   short="read", data={"private_text": PRIVATE, "prompt_tokens": 10})
    obs = run_observations([event, called])
    blob = json.dumps([dataclasses.asdict(o) for o in obs], default=str)
    assert PRIVATE not in blob
    assert "private_text_chars" in blob


def test_redaction_is_idempotent() -> None:
    """The guard redacts, then a sink may redact again: same result, no corruption."""
    once = redact.redact(dataclasses.asdict(hostile_turn()))
    twice = redact.redact(once)
    assert once == twice
    assert redact.redact_text(redact.redact_text(PHONE)) == redact.redact_text(PHONE)


def _calls(path: Path, names: set[str]) -> list[str]:
    out: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in names):
            out.append(ast.unparse(node)[:120])
    return out


def test_no_module_calls_an_exporter_around_the_guard() -> None:
    """A new sink that calls ``.export`` directly fails this test."""
    offenders: list[tuple[str, str]] = []
    for path in ROOT.rglob("*.py"):
        if path.name == "sinks.py":
            continue  # the door itself is where the one call lives
        for call in _calls(path, {"export"}):
            if not call.startswith(("sinks.export", "_sinks.export")):
                offenders.append((str(path.relative_to(ROOT)), call))
        for call in _calls(path, {"feedback"}):
            if "exporter" in call and not call.startswith(("sinks.feedback",
                                                           "_sinks.feedback")):
                offenders.append((str(path.relative_to(ROOT)), call))
    assert offenders == []


def test_only_the_langfuse_exporter_imports_the_sdk() -> None:
    """A second tracing vendor is a decision, not a drift: this test asks for it."""
    extra = []
    for path in ROOT.rglob("*.py"):
        if path.name == "langfuse_export.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "langfuse"
                                                    for a in node.names):
                extra.append(str(path.relative_to(ROOT)))
            elif (isinstance(node, ast.ImportFrom) and node.module
                  and node.module.split(".")[0] == "langfuse"):
                extra.append(str(path.relative_to(ROOT)))
    assert set(extra) <= {"harness/review_pocs.py", "observe/experiments.py"}, extra


def test_a_turn_book_export_is_guarded_too() -> None:
    rec = RecordingExporter()
    book = TurnBook(rec)
    with book.prelude("job-g", flow="receipt_reply", shop_id="shop-1", thread_id="th") as t:
        assert t is not None
        t.input = {"text": f"call {PHONE}"}
        t.output = PRIVATE
    book.close("job-g", "looks_fake")
    assert rec.turns and "8031234567" not in flatten(rec.turns[0])


def test_payload_of_covers_the_fields_an_exporter_sends() -> None:
    """If the exporter starts sending a new field, it must be in the check."""
    turn = hostile_turn()
    payload = sinks.payload_of(turn)
    for key in ("input", "output", "metadata", "observations", "scores"):
        assert key in payload
    assert payload["observations"][0]["input"]
    # The check catches a leak in any field, not only the top level.
    assert redact.find_leaks({"metadata": {"phone": PHONE}}) == ["metadata.phone: phone"]
    assert redact.find_leaks({"input": f"acct {ACCOUNT}"}) == ["input: long digits"]
