"""Amounts and dates from what shops say, and voice replies read from the text (RAI-290).

These are typed-text cases written for the reader; the 50 voice clips are in
``eval/voice_values`` (``python -m mimi.receipts.values_eval``).
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from mimi.receipts.inbox import ShopReply
from mimi.receipts.promises import message_for, pending_from, question_text, readback_text
from mimi.voice.promise import read_promise
from mimi.voice.render import figures, render_voice, text_language

WED = "2026-10-28T10:00:00+01:00"  # a Wednesday in Lagos
SAT = "2026-10-31T09:30:00+01:00"
D = date.fromisoformat


@pytest.mark.parametrize(("text", "kobo", "due"), [
    ("I will pay twenty-five thousand naira tomorrow", 2_500_000, "2026-10-29"),
    ("I go pay one hundred and twenty thousand naira on the fifth of November", 12_000_000,
     "2026-11-05"),
    ("Ma pay forty-five thousand naira on Saturday, ẹ jọ̀ọ́", 4_500_000, "2026-10-31"),
    ("I will pay ẹgbẹ̀rún márùn-ún by Friday", 500_000, "2026-10-30"),
    ("Màá san ẹgbẹ̀rún mẹ́ẹ̀ẹ́dógún náírà ní ọ̀la", 1_500_000, "2026-10-29"),
    ("Màá san ẹgbẹ̀rún méjìlá ní Ọjọ́ Àbámẹ́ta", 1_200_000, "2026-10-31"),
    ("Zan biya dubu biyu da ɗari biyar jibi", 250_000, "2026-10-30"),
    ("I will pay five hundred thousand naira on the thirtieth", 50_000_000, "2026-10-30"),
    ("I will pay two hundred and fifty thousand naira next Monday", 25_000_000, "2026-11-02"),
    ("one million two hundred thousand naira I go pay tomorrow", 120_000_000, "2026-10-29"),
    ("na saturday i go pay twelve thousand", 1_200_000, "2026-10-31"),
    ("nasaturday i go pay twelve thousand", 1_200_000, "2026-10-31"),
    ("ago pe 20k nex wik friday", 2_000_000, "2026-11-06"),
])
def test_clear_promises_are_typed_values(text: str, kobo: int, due: str) -> None:
    r = read_promise(text, WED)
    assert r.status == "promise", (text, r)
    assert r.amount_kobo == kobo and r.due_on == D(due)


def test_two_thousand_five_is_asked_never_guessed() -> None:
    for text in ("I will pay two thousand five on Friday", "Màá san 2005 lọ́la"):
        r = read_promise(text, WED)
        assert r.status == "unclear" and r.issue == "ambiguous_amount"
        assert r.amount_kobo is None and set(r.amount_options) == {250_000, 200_500}
    r = read_promise("I will pay ten thousand five on Monday", WED)
    assert set(r.amount_options) == {1_050_000, 1_000_500} and r.due_on == D("2026-11-02")
    # Said in full, or with a comma, there is nothing to ask.
    assert read_promise("I will pay two thousand five hundred naira Friday", WED).amount_kobo == \
        250_000
    assert read_promise("I will pay N2,005 on Friday", WED).amount_kobo == 200_500


def test_next_friday_on_a_wednesday_is_asked() -> None:
    r = read_promise("I will pay fifteen thousand naira next Friday", WED)
    assert r.status == "unclear" and r.issue == "ambiguous_date"
    assert r.amount_kobo == 1_500_000 and r.date_options == (D("2026-10-30"), D("2026-11-06"))
    # Said on a Saturday, the coming Friday is already next week.
    assert read_promise("I will pay fifteen thousand naira next Friday", SAT).due_on == \
        D("2026-11-06")
    assert read_promise("I go pay 30k Friday next week", WED).due_on == D("2026-11-06")
    assert read_promise("I go pay 30k this Friday", WED).due_on == D("2026-10-30")


def test_this_weekend_is_asked_with_both_days() -> None:
    r = read_promise("Oga I go balance you fifty thousand naira this weekend", WED)
    assert r.issue == "ambiguous_date" and r.date_options == (D("2026-10-31"), D("2026-11-01"))


def test_instalments_are_never_added_up() -> None:
    r = read_promise("I go pay ten thousand today and ten thousand on Friday", WED)
    assert r.status == "unclear" and r.amount_kobo != 2_000_000
    r = read_promise("I go pay ten thousand and ten thousand tomorrow", WED)
    assert r.amount_kobo != 2_000_000 and r.amount_kobo != 1_001_000


def test_bare_small_numbers_offer_both_readings() -> None:
    r = read_promise("Abeg I go pay 20 tomorrow", WED)
    assert r.issue == "ambiguous_amount" and set(r.amount_options) == {2_000, 2_000_000}


def test_yoruba_complete_is_not_a_pidgin_pay() -> None:
    assert read_promise("Gbogbo ẹrù ti pé", WED).status == "none"


def test_questions_name_both_options() -> None:
    q = question_text("ambiguous_amount", "dlv-1", None, D("2026-10-30"),
                      amount_options=(250_000, 200_500))
    assert "N2,500" in q and "N2,005" in q and "Friday 30 October 2026" in q
    q = question_text("ambiguous_date", "dlv-1", 1_500_000,
                      date_options=(D("2026-10-30"), D("2026-11-06")))
    assert "N15,000" in q and "Friday 30 October 2026" in q and "Friday 6 November 2026" in q
    reply = ShopReply("m1", "j", "s", "+2340000000000", "I will pay two thousand five Friday", WED)
    purpose, text = message_for(pending_from(read_promise(reply.text, WED), reply), "dlv-1")
    assert purpose == "promise_clarify" and "N2,500 or N2,005" in text


# ------------------------------------------------------------- voice replies


def test_the_voice_is_the_text_with_figures_in_words() -> None:
    text = readback_text("dlv-56-9039", 2_000_000, D("2026-10-29"))
    vs, why = render_voice(text, "english", today=D("2026-10-28"),
                           expect_kobo=[2_000_000], expect_days=[D("2026-10-29")])
    assert vs is not None, why
    assert vs.script.startswith("You said you will pay twenty thousand naira on Thursday, the "
                                "twenty-ninth of October, for this delivery. Reply YES")
    assert vs.amounts_kobo == (2_000_000,) and vs.days == (D("2026-10-29"),)
    assert not any(ch.isdigit() for ch in vs.script)
    # Everything but the figures and the reference is the text, word for word
    # (pacing only changes punctuation).
    def words(x: str) -> list[str]:
        return re.findall(r"[a-z]+", x.lower())

    tail = words(text.split("dlv-56-9039.", 1)[1])
    assert words(vs.script)[-len(tail):] == tail


def test_a_voice_that_would_say_another_figure_is_dropped() -> None:
    text = readback_text("dlv-1", 2_000_000, D("2026-10-29"))
    assert render_voice(text, "english", today=D("2026-10-28"), expect_kobo=[1_500_000])[0] is None
    assert render_voice(text, "english", today=D("2026-10-28"),
                        expect_days=[D("2026-10-30")])[0] is None
    # A weekday that is not the date's weekday: text only.
    vs, why = render_voice("Pay N45,000 by Saturday 30 October 2026.", "english",
                           today=D("2026-10-28"))
    assert vs is None and "Friday" in why
    # Kobo and numbers code cannot read for certain stay text only.
    assert render_voice("You owe N45,500.50.", "english", today=D("2026-10-28"))[0] is None
    assert render_voice("Send 20 tomorrow.", "english", today=D("2026-10-28"))[0] is None


def test_voices_follow_the_shops_choice_and_the_texts_language() -> None:
    text = "Your balance is N1,250,000. Please pay by tomorrow."
    for pref, voice in (("english", "english"), ("pidgin", "pidgin"), ("yoruba", "yoruba:en"),
                        ("igbo", "igbo:en"), ("hausa", "hausa:en")):
        vs, _ = render_voice(text, pref, today=D("2026-10-28"))
        assert vs is not None and vs.voice == voice
        assert "one million two hundred and fifty thousand naira" in vs.script
    assert text_language("Ẹ ṣé. Màá kọ ọ́ sílẹ̀, ẹ jọ̀ọ́.") == "yoruba"
    vs, _ = render_voice("Ẹ ṣé. Màá kọ ọ́ sílẹ̀, ẹ jọ̀ọ́.", "yoruba", today=D("2026-10-28"))
    assert vs is not None and vs.voice == "yoruba" and vs.script == "Ẹ ṣé. Màá kọ ọ́ sílẹ̀, ẹ jọ̀ọ́."
    # A Yoruba text with a figure is never spoken: digits read in Yoruba are too easy to mishear.
    assert render_voice("Ẹ ṣé, ẹ san N20,000 ní ọ̀la. Ẹ jọ̀ọ́.", "yoruba",
                        today=D("2026-10-28"))[0] is None


def test_figures_reads_text_and_script_the_same_way() -> None:
    k, d, doubt = figures("Pay N20,000 on Thursday 29 October 2026.", D("2026-10-28"))
    assert dict(k) == {2_000_000: 1} and set(d) == {D("2026-10-29")} and not doubt
    k2, d2, _ = figures("Pay twenty thousand naira on Thursday the twenty-ninth of October.",
                        D("2026-10-28"))
    assert k2 == k and set(d2) == set(d)


def test_the_values_eval_passes_its_safety_marks_on_the_recorded_run() -> None:
    from mimi.receipts import values_eval as ve

    assert ve.main(["--no-write"]) == 0
