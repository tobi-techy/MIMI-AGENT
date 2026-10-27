"""scripts/make_router_train.py: only well-formed generator answers become training rows."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "make_router_train.py"
_spec = importlib.util.spec_from_file_location("make_router_train", _PATH)
assert _spec and _spec.loader
mrt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mrt)


@pytest.mark.parametrize("raw", [
    '{"messages": "hello"}',          # a string, not a list (CodeRabbit on PR #10)
    '{"messages": ["ok", 5]}',        # a non-string item
    '{"messages": {"a": "b"}}',
    '{"msgs": ["hello"]}',
    "no json here",
    '{"messages": ["unterminated',
])
def test_a_malformed_answer_writes_no_rows(raw: str) -> None:
    assert mrt.parse_messages(raw) == []


def test_a_list_of_strings_is_kept_trimmed_and_without_blanks() -> None:
    raw = 'Here you go: {"messages": [" abeg wetin I owe ", "", "I go pay Friday"]}'
    assert mrt.parse_messages(raw) == ["abeg wetin I owe", "I go pay Friday"]
