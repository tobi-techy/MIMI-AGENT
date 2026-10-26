"""Payment promises read in code: integer kobo and an Africa/Lagos day."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from mimi.receipts.inbox import ShopReply
from mimi.receipts.promises import (
    MAX_PROMISE_MESSAGES,
    pending_from,
    promise_step,
    question_text,
    readback_text,
    scan_burst,
)
from mimi.voice.promise import (
    find_amounts,
    fold,
    is_yes,
    lagos_day,
    naira_words,
    read_promise,
)

WAT = timezone(timedelta(hours=1))
MON = datetime(2026, 10, 26, 14, 0, tzinfo=WAT)  # a Monday


def promise(text: str, at: datetime = MON) -> tuple[int, date]:
    r = read_promise(text, at)
    assert r.status == "promise", (text, r)
    assert r.promise is not None
    return r.promise.amount_kobo, r.promise.due_on


@pytest.mark.parametrize(
    ("text", "kobo", "due"),
    [
        ("I go pay 20k tomorrow", 2_000_000, date(2026, 10, 27)),
        ("make I pay 15,000 for Friday", 1_500_000, date(2026, 10, 30)),
        ("I go pay 20 k next tomorrow", 2_000_000, date(2026, 10, 28)),
        ("I will pay ₦20,000 on 30th October", 2_000_000, date(2026, 10, 30)),
        ("I will pay N20,000.50 on the 30th", 2_000_050, date(2026, 10, 30)),
        ("I will pay twenty thousand naira tomorrow", 2_000_000, date(2026, 10, 27)),
        ("I go pay twenty k on Friday", 2_000_000, date(2026, 10, 30)),
        ("I will pay fifteen thousand five hundred on Saturday", 1_550_000, date(2026, 10, 31)),
        ("I will pay two hundred and fifty thousand naira on Friday", 25_000_000, date(2026, 10, 30)),
        ("I go pay 2.5k Monday next week", 250_000, date(2026, 11, 2)),
        ("All don land. I go pay 30k next week Monday.", 3_000_000, date(2026, 11, 2)),
        ("I will pay fifty thousand naira at the end of the month", 5_000_000, date(2026, 10, 31)),
        ("I go pay 20,000 naira 2/11", 2_000_000, date(2026, 11, 2)),
        # Yoruba, Igbo, Hausa, and code-switching
        ("Màá san ẹgbẹ̀rún mẹ́wàá náírà ní ọ̀la", 1_000_000, date(2026, 10, 27)),
        ("Mo máa san ẹgbẹ̀rún lọ́nà ogún lónìí", 2_000_000, date(2026, 10, 26)),
        ("Everything ti dé. Màá san twenty thousand naira ní Friday.", 2_000_000, date(2026, 10, 30)),
        ("Aga m akwụ puku naira iri abụọ echi", 2_000_000, date(2026, 10, 27)),
        ("Aga m akwụ puku iri na ise na Fraịdee", 1_500_000, date(2026, 10, 30)),
        ("Zan biya naira dubu ashirin gobe", 2_000_000, date(2026, 10, 27)),
        ("Zan biya dubu goma sha biyar ranar Juma'a", 1_500_000, date(2026, 10, 30)),
        ("Zan biya fifteen thousand naira jibi", 1_500_000, date(2026, 10, 28)),
    ],
)
def test_reads_amount_in_kobo_and_day_in_lagos(text: str, kobo: int, due: date) -> None:
    assert promise(text) == (kobo, due)


def test_day_comes_from_the_message_time_in_lagos() -> None:
    # 23:30 UTC on Monday is 00:30 Tuesday in Lagos: "tomorrow" is Wednesday.
    late = datetime(2026, 10, 26, 23, 30, tzinfo=timezone.utc)
    assert lagos_day(late) == date(2026, 10, 27)
    assert promise("I go pay 20k tomorrow", late)[1] == date(2026, 10, 28)
    assert promise("I go pay 20k tomorrow", "2026-10-26T14:00:00+01:00")[1] == date(2026, 10, 27)


@pytest.mark.parametrize(
    ("text", "issue"),
    [
        ("I go pay 20 tomorrow", "ambiguous_amount"),
        ("I will pay fifteen or twenty thousand on Friday", "several_amounts"),
        ("I go pay 10k or 15k Friday", "several_amounts"),
        ("Everything is complete. I will pay you twenty thousand for it.", "no_date"),
        ("Two cartons did not come. I will pay for the rest on Friday.", "no_amount"),
        ("Everything reach. I go pay you Friday.", "no_amount"),
        ("I go pay 20k next week", "ambiguous_date"),
        ("I go pay 20k today or tomorrow", "several_dates"),
        ("I go pay 20k Monday", "ambiguous_date"),  # said on a Monday: today or next week?
        ("Màá san ẹgbẹ̀rún márùn-ún", "no_date"),
        ("Màá san ẹgbẹ̀rún mewarun ní ọ̀la", "ambiguous_amount"),  # a misheard number
    ],
)
def test_unclear_promises_name_what_is_missing(text: str, issue: str) -> None:
    r = read_promise(text, MON)
    assert r.status == "unclear" and r.promise is None
    assert r.issue == issue


def test_paid_claims_and_plain_messages_are_not_promises() -> None:
    assert read_promise("I don pay the money yesterday, check am.", MON).status == "paid_claim"
    assert read_promise("Mo ti san owó náà", MON).status == "paid_claim"
    assert read_promise("I have paid 20k", MON).status == "paid_claim"
    for text in (
        "Two cartons of the noodles are short. I counted 4 cartons, not 6.",
        "Yes, I have received everything. Thank you.",
        "Abeg, when una go bring the new price list?",
        "Ẹ ṣé o, gbogbo ẹrù ti dé pé.",
        "Na gode, duk kayan sun iso.",
    ):
        assert read_promise(text, MON).status == "none", text
    # Paid part of it, promises the rest: the promise is the rest only.
    assert promise("I don pay 10k, I go pay the rest 15k tomorrow") == (1_500_000, date(2026, 10, 27))


def test_counts_refs_times_and_phones_are_not_money() -> None:
    f = fold("Ref dlv-56-9039: 4 cartons, by 4pm, call +2340000000001, I go pay 20k")
    assert [a.kobo for a in find_amounts(f)] == [2_000_000]
    assert promise("Noodles no complete, na 4 carton. I go pay 15,000 Friday") == (
        1_500_000,
        date(2026, 10, 30),
    )


def test_a_low_transcript_is_never_a_promise() -> None:
    r = read_promise("I go pay 20k tomorrow", MON, transcript_ok=False)
    assert r.status == "unclear" and r.issue == "low_quality_transcript"


def test_answers_fill_what_is_missing() -> None:
    r = read_promise("Friday", MON, answering=True, known_kobo=2_000_000)
    assert r.status == "promise" and r.due_on == date(2026, 10, 30)
    r = read_promise("15k", MON, answering=True, known_due=date(2026, 10, 30))
    assert r.status == "promise" and r.amount_kobo == 1_500_000


def test_yes_is_strict() -> None:
    for t in ("YES", "yes o", "Yes sir", "Bẹ́ẹ̀ ni", "Ee", "Eh", "Na'am", "Yes, 20k Friday"):
        assert is_yes(t), t
    for t in ("ok", "👍", "no", "yes but make am Saturday", "Yes not 20k", "maybe"):
        assert not is_yes(t), t


def test_naira_words() -> None:
    assert naira_words(2_000_000) == "twenty thousand naira"
    assert naira_words(1_550_000) == "fifteen thousand five hundred naira"
    assert naira_words(25_000_050) == "two hundred and fifty thousand naira and fifty kobo"
    assert naira_words(100_500) == "one thousand and five naira"


def test_mimi_texts_are_plain_english_with_the_figures() -> None:
    text = readback_text("dlv-1", 2_000_000, date(2026, 10, 27))
    assert "N20,000" in text and "Tuesday 27 October 2026" in text and "Reply YES" in text
    assert "do not take or move money" in text
    assert promise(text) == (2_000_000, date(2026, 10, 27))  # it reads back exactly
    assert "N20,000" in question_text("no_date", "dlv-1", 2_000_000)
    assert "How much" in question_text("no_amount", "dlv-1")
    assert "type" in question_text("low_quality_transcript", "dlv-1")


def reply(text: str, mid: str, *, at: datetime = MON, kind: str = "text", status: str = "") -> ShopReply:
    return ShopReply(
        message_id=mid,
        job_id="rcpt-dlv-1",
        shop_id="shop-1",
        sender="+2340000000001",
        text=text,
        received_at=at.isoformat(),
        kind=kind,  # type: ignore[arg-type]
        transcript_status=status,
    )


def readback(text: str) -> dict:
    found = scan_burst([reply(text, "m1", kind="voice", status="ok")])
    assert found is not None
    p = pending_from(*found)
    return {**p, "asked": "readback" if p["status"] == "promise" else "question", "sent": 1}


def test_yes_to_the_readback_records() -> None:
    p = readback("I go pay 20k tomorrow")
    step = promise_step(p, [reply("YES", "m2", at=MON + timedelta(minutes=5))])
    assert step.action == "record" and step.witness is not None and step.witness.text == "YES"


def test_yes_with_a_new_amount_reads_back_again() -> None:
    p = readback("I go pay 20k tomorrow")
    step = promise_step(p, [reply("Yes 15k", "m2")])
    assert step.action == "read_back" and step.pending["amount_kobo"] == 1_500_000


def test_no_or_anything_else_asks_and_never_records() -> None:
    p = readback("I go pay 20k tomorrow")
    for words in ("No", "ok", "👍", "let me see"):
        step = promise_step(p, [reply(words, "m2")])
        assert step.action == "ask", words
        assert step.pending["issue"] == "correction"


def test_a_doubtful_voice_yes_never_records() -> None:
    p = readback("I go pay 20k tomorrow")
    step = promise_step(p, [reply("yes", "m2", kind="voice", status="low")])
    assert step.action == "ask" and step.pending["issue"] == "low_quality_transcript"
    step = promise_step(p, [reply("", "m2", kind="voice", status="failed")])
    assert step.action == "ask"


def test_question_then_answer_then_yes() -> None:
    p = readback("Everything complete. I will pay you twenty thousand for it.")
    assert p["status"] == "unclear" and p["issue"] == "no_date"
    step = promise_step(p, [reply("Friday", "m2")])
    assert step.action == "read_back"
    assert (step.pending["amount_kobo"], step.pending["due_on"]) == (2_000_000, "2026-10-30")
    p2 = {**step.pending, "asked": "readback", "sent": 2}
    assert promise_step(p2, [reply("yes", "m3")]).action == "record"


def test_message_limit_gives_up_without_recording() -> None:
    p = {**readback("I go pay 20k tomorrow"), "sent": MAX_PROMISE_MESSAGES}
    assert promise_step(p, [reply("No", "m2")]).action == "give_up"
