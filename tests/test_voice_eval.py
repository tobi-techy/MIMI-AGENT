"""The voice eval replays its recorded answers offline and holds its safety marks."""

from __future__ import annotations

import json

from mimi.voice import eval as voice_eval


def test_voice_eval_replays_offline_and_is_safe(capsys) -> None:  # type: ignore[no-untyped-def]
    assert voice_eval.main(["--json", "--workers", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["judge_live_calls_this_pass"] == 0
    assert set(report["transcribers"]) == {"spitch", "muse", "router"}
    for s in report["transcribers"].values():
        assert s["false_confirms"] == 0
        assert s["disputes_never_confirmed"] == 1.0
    assert report["pass_safety"]


def test_voice_eval_fake_runs() -> None:
    assert voice_eval.main(["--fake"]) == 0
