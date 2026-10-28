"""The style guard (RAI-303): no dashes, no filler, and never a changed figure."""

from __future__ import annotations

import ast
import random
from pathlib import Path

import pytest

from mimi.harness import style
from mimi.harness.style import banned_in, clean, clean_reply, figures, has_dash

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("raw, want", [
    ("Mama Nkechi owes N184,500 \u2014 due 30 Oct.", "Mama Nkechi owes N184,500, due 30 Oct."),
    ("Your balance is N12,000 \u2013 please pay by Friday.",
     "Your balance is N12,000, please pay by Friday."),
    ("Payment is pending\u2014I'll check again at 4:30 pm.",
     "Payment is pending, I'll check again at 4:30 pm."),
    ("Tap 0\u20132, 3\u20135 or 6+.", "Tap 0-2, 3-5 or 6+."),
    ("Mon\u2013Fri 8am\u20136pm", "Mon-Fri 8am-6pm"),
    ("Okay -- noted.", "Okay, noted."),
    ("Your order (4 cartons \u2014 about N48,000) is with the owner.",
     "Your order (4 cartons, about N48,000) is with the owner."),
    ("\u2014 4 ctn Peak Milk: N48,000\n\u2014 2 ctn Indomie: N12,500\nTotal: N60,500",
     "- 4 ctn Peak Milk: N48,000\n- 2 ctn Indomie: N12,500\nTotal: N60,500"),
    ("Thanks \u2014", "Thanks"),
    ("Noted: \u2014 the owner will call.", "Noted: the owner will call."),
])
def test_dashes_are_replaced(raw: str, want: str) -> None:
    out = clean_reply(raw)
    assert out == want
    assert not has_dash(out)


@pytest.mark.parametrize("raw, want", [
    ("I'd be happy to help! Mama Nkechi owes N184,500.", "Mama Nkechi owes N184,500."),
    ("I\u2019d be happy to help with that. Delivery DLV-0042 is confirmed.",
     "Delivery DLV-0042 is confirmed."),
    ("As an AI, I can't see your bank. I've sent it to Coral Lane Trading.",
     "I can't see your bank. I've sent it to Coral Lane Trading."),
    ("Certainly! Delivery DLV-0042 is confirmed. Please don't hesitate to reach out if you "
     "have any questions.", "Delivery DLV-0042 is confirmed."),
    ("Hello Mama Bisi, I hope this message finds you well. Invoice INV-0012 has N5,000 "
     "outstanding.", "Hello Mama Bisi. Invoice INV-0012 has N5,000 outstanding."),
    ("Thanks for your patience. The payment of N20,000 is pending.",
     "The payment of N20,000 is pending."),
    ("Great question! Your limit is set by the owner.", "Your limit is set by the owner."),
    ("Your payment is pending. Feel free to reach out if anything changes.",
     "Your payment is pending."),
    ("I've sent it to the owner. Let me know if there's anything else I can help with.",
     "I've sent it to the owner."),
    ("You are welcome, Ngozi Stores. Glad to help anytime.", "You are welcome, Ngozi Stores."),
])
def test_filler_is_removed(raw: str, want: str) -> None:
    out = clean_reply(raw)
    assert out == want
    assert not banned_in(out)


def test_plain_messages_are_untouched() -> None:
    for text in (
        "Thanks. I can see N84,500 from TITI OWOLABI on your screenshot.",
        "Coral Lane Trading delivery receipt for Mama Nkechi Stores\nRef DLV-0042, 2026-10-28\n"
        "- 4 ctn Peak Milk 400g: N48,000\nTotal: N48,000\nReply YES to confirm.",
        "How many cartons of Sunrise Instant Noodles 70g do you have left? 0-2 | 3-5 | 6+",
        "Make I sure: you wan cancel your restock order? Reply YES or NO.",
        "Happy new month! Your invoice INV-0012 is due on Friday 30 October.",
    ):
        c = clean(text)
        assert c.text == text and not c.changed


def test_a_message_that_is_only_filler_is_kept_not_emptied() -> None:
    c = clean("I'd be happy to help.")
    assert c.text == "I'd be happy to help." and c.kept_original


def test_figures_never_change_property() -> None:
    """Random messages with figures, dashes and filler: the figures, in order, are identical."""
    rng = random.Random(303)
    parts = ["N184,500", "N12,000.50", "30 Oct", "4:30 pm", "INV-2026-0012", "DLV-0042",
             "0803 123 4567", "2026-10-30", "6+", "3-5", "\u2014", "\u2013", " -- ",
             "I'd be happy to help!", "As an AI,", "Certainly!", "Great question.",
             "Please don't hesitate to reach out.", "Mama Bisi", "owes", "is due", ",", ".",
             "\n", "Thanks for your patience.", "paid", "the owner"]
    for _ in range(3000):
        text = " ".join(rng.choice(parts) for _ in range(rng.randint(1, 14)))
        out = clean_reply(text)
        assert figures(out) == figures(text), (text, out)
        assert not has_dash(out), (text, out)
        if text.strip(" \n,.-\u2014\u2013"):
            assert out.strip(), text


def test_names_and_references_survive() -> None:
    text = ("Mama Nkechi-Okafor \u2014 ref KST/TRF/0042-A \u2014 sent N5,000 to Coral Lane "
            "Trading on 28/10/2026.")
    out = clean_reply(text)
    assert "Mama Nkechi-Okafor" in out and "KST/TRF/0042-A" in out
    assert figures(out) == figures(text)


def test_the_guard_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_STYLE_GUARD", "off")
    assert clean_reply("Pending \u2014 sorry.") == "Pending \u2014 sorry."
    monkeypatch.delenv("MIMI_STYLE_GUARD")
    assert style.enabled()


def test_send_to_user_and_ask_question_are_guarded() -> None:
    from datetime import datetime, timezone

    from mimi.harness.builtins import (
        AskQuestionIn,
        SendToUserIn,
        ask_question,
        send_to_user,
    )
    from mimi.harness.registry import ToolContext

    ctx = ToolContext.__new__(ToolContext)
    object.__setattr__(ctx, "now", datetime(2026, 10, 28, 12, tzinfo=timezone.utc))
    out = send_to_user(ctx, SendToUserIn(channel="owner_console",
                                         body="I'd be happy to help! It's N5,000 \u2014 due Friday."))
    assert out.body == "It's N5,000, due Friday."
    q = ask_question(ctx, AskQuestionIn(question="Which shop \u2014 Bisi or Ngozi?"))
    assert q.question == "Which shop, Bisi or Ngozi?"


def _strings(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))
    return out


def test_no_code_template_has_a_dash_or_filler() -> None:
    """Every string Mimi's code can send is dash-free and filler-free at the source."""
    templates = ["mimi/receipts/receipt.py", "mimi/receipts/tools.py", "mimi/receipts/promises.py",
                 "mimi/proof/copy.py", "mimi/stock/copy.py", "mimi/credit/copy.py",
                 "mimi/harness/collector.py", "mimi/harness/steer.py", "mimi/harness/loop.py",
                 "mimi/api/voice.py", "mimi/api/offline.py", "mimi/api/helpers.py",
                 "mimi/api/agent.py", "mimi/runtime/budgets.py", "mimi/brand/tools.py"]
    bad = []
    for rel in templates:
        for line, text in _strings(ROOT / rel):
            if has_dash(text):
                bad.append(f"{rel}:{line} dash")
            if "No filler:" in text:
                continue  # the prompt rule that names the banned phrases
            for p in banned_in(text):
                bad.append(f"{rel}:{line} {p!r}")
    assert not bad, bad
