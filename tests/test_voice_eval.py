"""The voice eval replays its recorded answers offline and holds its safety marks."""

from __future__ import annotations

import json

from mimi.voice import eval as voice_eval


def test_voice_eval_replays_offline_and_is_safe(capsys) -> None:  # type: ignore[no-untyped-def]
    assert voice_eval.main(["--json", "--workers", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["judge_live_calls_this_pass"] == 0
    names = {"spitch", "muse", "router"}
    assert set(report["transcribers"]) == names | {f"{n}/no-hint" for n in names}
    assert len(report["clips"]) == 50
    for s in report["transcribers"].values():
        assert s["false_confirms"] == 0
        assert s["disputes_never_confirmed"] == 1.0
        assert s["promise_recorded_without_yes"] == 0
        assert s["promise_wrong_recorded"] == 0
        assert s["promises_heard_where_none"] == 0
    router = report["transcribers"]["router"]
    assert router["promise_amount_date_accuracy"] >= 0.9
    assert router["promise_recorded_right_after_answer"] == router["promise_readbacks_answered"]
    assert report["cost"]["spitch_usd_total"] < 0.5
    assert report["pass_safety"]
    assert report["passes"]["no_wrong_promise_recorded_all_transcribers"]


def test_strict_fails_on_the_first_read_dispute_mark() -> None:
    # Tobi's call (26 Oct): a dispute missed on the first read is a warning;
    # --strict makes it fail.
    assert voice_eval.main(["--workers", "1", "--strict"]) == 1


def test_voice_eval_fake_runs() -> None:
    assert voice_eval.main(["--fake"]) == 0
