"""Second opinion on amounts and days, money words, and voice pacing (RAI-290)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

import pytest

from mimi.voice.base import AudioClip, Transcript, VoiceContext
from mimi.voice.consensus import SECOND_OPINION, choose, reconcile
from mimi.voice.pacing import pace
from mimi.voice.render import figures, render_voice
from mimi.voice.router import DEFAULT_POLICY, VoiceRouter
from mimi.voice.vocab import PROMISE_VOCAB, VOCAB_PROVIDERS, with_vocab

WED = "2026-10-28T10:00:00+01:00"
THU = date(2026, 10, 29)


def ok(*texts: str) -> list[tuple[str, bool]]:
    return [(t, True) for t in texts]


def test_two_transcripts_that_agree_give_the_promise() -> None:
    r = reconcile(ok("I go pay 20k tomorrow", "I go pay twenty thousand naira tomorrow"), WED)
    assert r.status == "promise" and r.amount_kobo == 2_000_000 and r.due_on == THU


def test_a_figure_only_one_transcriber_heard_stands_when_the_other_heard_none() -> None:
    # The primary lost the amount; the second opinion heard it and nothing else.
    r = reconcile(ok("I go pay you tomorrow", "I go pay you 15k tomorrow"), WED)
    assert r.status == "promise" and r.amount_kobo == 1_500_000


def test_two_different_amounts_are_asked_never_picked() -> None:
    r = reconcile(ok("I go pay 20k tomorrow", "I go pay 25k tomorrow"), WED)
    assert r.status == "unclear" and r.issue == "ambiguous_amount"
    assert set(r.amount_options) == {2_000_000, 2_500_000}
    assert r.amount_kobo is None and r.due_on == THU


def test_an_unreadable_amount_against_a_clean_one_is_asked() -> None:
    # Spitch heard something it cannot read; Muse heard a clean but different number.
    r = reconcile(ok("ma san egberun merun run naira ni ola", "ma san egberun merin naira ni ola"), WED)
    assert r.status == "unclear" and r.amount_kobo is None and not r.amount_options


def test_two_different_days_are_asked() -> None:
    r = reconcile(ok("I go pay 20k on Friday", "I go pay 20k on Saturday"), WED)
    assert r.status == "unclear" and r.issue == "ambiguous_date"
    assert set(r.date_options) == {date(2026, 10, 30), date(2026, 10, 31)}


def test_next_week_agrees_with_a_day_inside_next_week() -> None:
    r = reconcile(ok("make i pay 30 k for fried next week", "make I pay thirty k for Friday next week"), WED)
    assert r.status == "promise" and r.due_on == date(2026, 11, 6)
    # A day outside next week does not agree with "next week".
    r = reconcile(ok("make i pay 30 k next week", "make I pay thirty k tomorrow"), WED)
    assert r.status == "unclear"


def test_words_that_fit_two_values_stay_a_question() -> None:
    r = reconcile(ok("I will pay two thousand five on Friday", "I will pay 2,500 on Friday"), WED)
    assert r.status == "unclear" and set(r.amount_options) == {250_000, 200_500}


def test_a_paid_claim_from_the_primary_is_never_turned_into_a_promise() -> None:
    r = reconcile(ok("i don pay yu yesterday", "I don't pay you yesterday, I go pay 5k tomorrow"), WED)
    assert r.status == "paid_claim"


def test_a_doubtful_second_transcript_adds_nothing() -> None:
    r = reconcile([("I go pay 20k tomorrow", True), ("I go pay 90k tomorrow", False)], WED)
    assert r.status == "promise" and r.amount_kobo == 2_000_000


def t(text: str, provider: str, status: str = "ok") -> Transcript:
    return Transcript(text=text, status=status, provider=provider, tried=(f"{provider}:{status}",))  # type: ignore[arg-type]


def test_choose_keeps_the_transcript_that_carries_the_agreed_reading() -> None:
    out = choose([t("make i pay 30 k for fried next week", "spitch"),
                  t("make I pay thirty k for Friday next week", "muse")], WED)
    assert out.provider == "muse" and out.status == "ok"
    assert out.tried == ("spitch:ok", "muse:ok")


def test_choose_marks_a_disagreement_low_when_no_transcript_fits() -> None:
    out = choose([t("I go pay 20k tomorrow", "spitch"), t("I go pay 25k tomorrow", "muse")], WED)
    assert out.status == "low" and "disagree" in out.reason


def test_choose_keeps_the_question_when_one_transcript_asks_it() -> None:
    out = choose([t("ma san egberun merun run naira ni ola", "spitch"),
                  t("ma san egberun merin naira ni ola", "muse")], WED)
    # Spitch's own reading asserts no amount, only the agreed day: Mimi asks how much.
    assert out.provider == "spitch" and out.status == "ok"


def test_choose_leaves_messages_that_are_not_about_paying_alone() -> None:
    out = choose([t("the goods don land, thank you", "spitch"), t("the goods don't land", "muse")], WED)
    assert out.provider == "spitch" and out.text == "the goods don land, thank you"


@dataclass
class Scripted:
    name: str
    text: str
    seen: list[VoiceContext] = field(default_factory=list)

    def transcribe(self, clip: AudioClip, ctx: VoiceContext) -> Transcript:
        self.seen.append(ctx)
        return t(self.text, self.name)


CLIP = AudioClip(b"OggS" + b"\0" * 64, "ogg", 2.0)


def test_router_asks_the_second_opinion_only_where_the_policy_names_one() -> None:
    sp = Scripted("spitch", "make i pay 30 k for fried next week")
    mu = Scripted("muse", "make I pay thirty k for Friday next week")
    r = VoiceRouter({"spitch": sp, "muse": mu}, DEFAULT_POLICY, second=SECOND_OPINION,
                    vocab_providers=VOCAB_PROVIDERS)
    out = r.transcribe(CLIP, VoiceContext(language_hint="pcm", said_at=WED))
    assert out.provider == "muse" and r.second_opinions == 1
    assert sp.seen[-1].keywords == ()  # Spitch gets no money words unless turned on
    assert mu.seen[-1].keywords == PROMISE_VOCAB["pcm"]
    # Igbo: Spitch alone.
    r.transcribe(CLIP, VoiceContext(language_hint="ig", said_at=WED))
    assert r.second_opinions == 1 and len(mu.seen) == 1
    # Without the option the router is exactly the old one.
    plain = VoiceRouter({"spitch": sp, "muse": mu}, DEFAULT_POLICY)
    assert plain.transcribe(CLIP, VoiceContext(language_hint="pcm")).provider == "spitch"
    assert sp.seen[-1].keywords == () and len(mu.seen) == 1


def test_money_words_keep_the_receipt_words_first() -> None:
    assert with_vocab(("Indomie",), "en")[0] == "Indomie"
    assert with_vocab((), None) == ()
    assert "naira" in with_vocab((), "yo")


def test_env_router_turns_the_second_opinion_on_by_default() -> None:
    from mimi.voice.env import transcriber_from_env

    tr = transcriber_from_env({"SPITCH_API_KEY": "x", "META_API_KEY": "y"})
    assert isinstance(tr, VoiceRouter) and tr.second == SECOND_OPINION
    assert tr.vocab_providers == ("muse",)
    off = transcriber_from_env({"SPITCH_API_KEY": "x", "META_API_KEY": "y", "MIMI_VOICE_SECOND": "0",
                                "MIMI_SPITCH_VOCAB": "1"})
    assert isinstance(off, VoiceRouter) and not off.second and "spitch" in off.vocab_providers


@pytest.mark.parametrize(
    ("script", "want"),
    [
        ("You will pay twenty five thousand naira on Thursday the twenty-ninth of October for "
         "this delivery. I only note it; I do not move money",
         "You will pay twenty-five thousand naira on Thursday, the twenty-ninth of October, for "
         "this delivery. I only note it. I do not move money."),
        ("Pay forty eight thousand five hundred naira \u2014 by Saturday & reply",
         "Pay forty-eight thousand five hundred naira, by Saturday and reply."),
    ],
)
def test_pacing_breaks_sentences_and_says_numbers_as_one(script: str, want: str) -> None:
    assert pace(script) == want


def test_pacing_never_writes_a_dash_and_keeps_every_figure() -> None:
    today = date(2026, 10, 28)
    s = "You will pay twenty five thousand naira on Thursday the twenty-ninth of October; thanks"
    p = pace(s)
    assert "\u2014" not in p and "\u2013" not in p
    assert figures(p, today)[:2] == figures(s, today)[:2]


def test_paced_voice_reply_still_carries_the_texts_figures() -> None:
    text = ("You said you will pay N48,500 on Thursday 29 October 2026 for delivery dlv-1-2. "
            "Reply YES if that is right; I do not take or move money.")
    vs, why = render_voice(text, "english", today=date(2026, 10, 28), expect_kobo=[4_850_000],
                           expect_days=[THU])
    assert vs is not None, why
    assert "forty-eight thousand five hundred naira" in vs.script
    assert "Thursday, the twenty-ninth of October," in vs.script
    assert "dlv" not in vs.script and ";" not in vs.script and "\u2014" not in vs.script


def test_fresh_held_out_labels_cover_every_language_and_are_never_shown() -> None:
    from mimi.receipts import values_eval as ve

    fresh = [c for c in ve.load_clips() if c["split"] == "fresh"]
    assert len(fresh) >= 50
    assert {c["lang"] for c in fresh} == {"english", "pidgin", "yoruba_english", "yoruba", "igbo", "hausa"}
    assert len({c["tts"]["voice"] for c in fresh}) >= 12
    assert all(c["synthetic"] for c in fresh)
    ids = [c["id"] for c in ve.load_clips()]
    assert len(ids) == len(set(ids))
    assert ve.main(["--split", "fresh", "--show", "--no-write"]) == 2
    json.dumps(fresh)
