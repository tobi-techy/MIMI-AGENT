"""When-to-speak eval (RAI-300): 80 labelled wake-up cases, synthetic.

    python -m mimi.harness.speak_eval [--no-write]

Offline and free: scripted runs over the synthetic Coral Lane world. Each
case is a wake-up (a direct question, an approval, money at risk, a routine
with or without news, a duplicate, an over-the-cap turn) with the tools the
model calls first and the message it tries to send. The speak gate decides
in ``enforce`` mode: must-speak cases must send (speak recall 100%,
including direct questions, approvals and money at risk), quiet cases must
send nothing (quiet precision >= 90%), and no case may send the same update
twice (zero duplicate updates). Every wake-up ends in a useful message or a
logged silence: each case checks the speak decision and its reason on the
trace. Pass marks block CI (RAI-229).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mimi.evalmarks import exit_code, passed, slice_rate
from mimi.harness import speak as S
from mimi.harness.collector import collector_helper, default_registry
from mimi.harness.gateway import Gateway, ScriptedModel, say
from mimi.harness.go import FakeGo
from mimi.harness.loop import Harness
from mimi.harness.policy import GoPolicy
from mimi.harness.types import RunEvent, RunResult
from mimi.world.loader import load_world

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval" / "speak"
REPORT = EVAL_DIR / "speak.report.json"
AT = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)  # Monday 26 October 2026, 09:00 WAT
WORLD = load_world()
TENANT = WORLD.tenant_id

Call = tuple[str, dict[str, Any]]


@dataclass(frozen=True)
class SpeakCase:
    cid: str
    kind: str  # question | approval | risk | news | quiet | duplicate | cap | digest
    # What the turn should do: "speak" sends the message, "quiet" sends
    # nothing (done_quiet), "digest" holds it for the daily note.
    want: str
    text: str  # the message the model tries to send
    first: tuple[Call, ...] = ()  # tools called in the step before the message
    alongside: tuple[Call, ...] = ()  # tools in the same model turn as the message
    shop_text: str = ""  # the shop's own message (untrusted) that woke the run
    owner_text: str = "how is shop-001 doing?"
    channel: str = "owner_console"
    wake: str = ""
    shop: str = "shop-001"
    # Pre-spent unprompted counts before this turn (the cap cases).
    spent: int = 0
    # A second send_to_user with the same body in the same turn (duplicates).
    repeat: bool = False
    expect: dict[str, Any] = field(default_factory=dict)


BAL1 = ("get_balance", {"shop_id": "shop-001"})
INV5 = ("get_invoices", {"shop_id": "shop-005"})
PAY1 = ("get_payment_history", {"shop_id": "shop-001"})
CHECK1 = ("check_payment", {"shop_id": "shop-001", "amount_kobo": 44_500_000})
CHECK5 = ("check_payment", {"shop_id": "shop-005", "amount_kobo": 400_000})
REMIND = ("send_reminder", {"shop_id": "shop-005", "tone": "soft",
                            "invoice_id": "dlv-00-0055"})
FOLLOW = ("set_followup", {"shop_id": "shop-005", "in_hours": 96, "reason": "promised Friday"})
FLAG = ("flag_payment_proof", {"shop_id": "shop-005", "reason": "fake alert",
                               "payment_id": "pay-00001"})


def _body(kind: str, shop: str, channel: str) -> dict[str, Any]:
    args: dict[str, Any] = {"channel": channel, "body": "", "ends_turn": True}
    if channel == "shop_whatsapp":
        args["shop_id"] = shop
    return args


_CASES: list[SpeakCase] = []

# ---- direct questions: always answered (100%), owner and shop (20)
_owner_qs = [
    "how much does shop-001 owe?", "who owes over 50k?", "chase everyone over 50k",
    "what payments came in today?", "which shops promised to pay this week?",
    "is Funke & Sons Mini Mart overdue?", "summarise shop-001 for me",
    "has shop-005 paid?", "what is my total overdue?", "show me the flagged alerts",
]
for i, q in enumerate(_owner_qs):
    _CASES.append(SpeakCase(f"q-owner-{i + 1:02d}", "question", "speak",
                            "Mama Sola Provisions owes N3,497,300, with N2,387,250 overdue.",
                            (BAL1,), owner_text=q))
_shop_qs = [
    "how much do I owe?", "abeg wetin be my balance?", "have you seen my payment?",
    "when is my next delivery?", "na how much I dey owe?", "did my transfer land?",
    "what is my credit limit?", "why my balance high?", "mo fe mo iye ti mo je",
    "nawa oo, how much remain?",
]
for i, q in enumerate(_shop_qs):
    _CASES.append(SpeakCase(f"q-shop-{i + 1:02d}", "question", "speak",
                            "Mama Sola Provisions owes N3,497,300, with N2,387,250 overdue.",
                            (BAL1,), shop_text=q,
                            channel="shop_whatsapp"))

# ---- approvals and money at risk: always speak (12)
for i in range(6):
    _CASES.append(SpeakCase(f"appr-{i + 1:02d}", "approval", "speak",
                            "Mama Sola Provisions owes N3,497,300, N2,387,250 of it overdue. "
                            "Approve and send?",
                            (BAL1,), owner_text="chase everyone over 50k"))
for i in range(6):
    _CASES.append(SpeakCase(f"risk-{i + 1:02d}", "risk", "speak",
                            "That alert looks fake: we could not verify N4,000 for Funke & Sons "
                            "Mini Mart. I have flagged it for you.", (INV5, CHECK5),
                            shop_text="I don pay", channel="shop_whatsapp", shop="shop-005"))

# ---- real news: new money facts and real changes speak (12)
for i in range(6):
    _CASES.append(SpeakCase(f"news-owner-{i + 1:02d}", "news", "speak",
                            "Mama Sola Provisions owes N3,497,300, with N2,387,250 overdue.",
                            (BAL1,), owner_text="how is shop-001 doing?"))
for i in range(6):
    _CASES.append(SpeakCase(f"news-shop-{i + 1:02d}", "news", "speak",
                            "Mama Sola Provisions owes N3,497,300, with N2,387,250 overdue.",
                            (BAL1,), shop_text="abeg wetin be my balance?",
                            channel="shop_whatsapp"))

# ---- quiet: routines with no new facts send nothing (20)
for i in range(10):
    _CASES.append(SpeakCase(f"quiet-routine-{i + 1:02d}", "quiet", "quiet",
                            "Routine check done, all quiet.", (),
                            wake="routine" if i % 2 == 0 else "scheduled_check_in",
                            owner_text=""))
for i in range(6):
    _CASES.append(SpeakCase(f"quiet-nothing-{i + 1:02d}", "quiet", "quiet",
                            "Routine check done, nothing new to report.", (),
                            wake="routine", owner_text=""))
for i in range(4):
    _CASES.append(SpeakCase(f"quiet-dup-{i + 1:02d}", "duplicate", "quiet",
                            "Mama Sola Provisions owes N3,497,300.", (BAL1,),
                            owner_text="how is shop-001 doing?", repeat=True))

# ---- digest: clean routines wait for the daily note (8)
for i in range(8):
    _CASES.append(SpeakCase(f"digest-clean-{i + 1:02d}", "digest", "digest",
                            "14 payments matched clean, nothing flagged.", (INV5,),
                            wake="routine", owner_text=""))

# ---- cap: the 4th unprompted routine digest item waits (8)
for i in range(4):
    _CASES.append(SpeakCase(f"cap-owner-{i + 1:02d}", "cap", "digest",
                            "14 payments matched clean, nothing flagged.", (INV5,),
                            wake="routine", owner_text="", spent=3))
for i in range(4):
    _CASES.append(SpeakCase(f"cap-shop-{i + 1:02d}", "cap", "digest",
                            "14 payments matched clean, nothing flagged.", (INV5,),
                            wake="routine", owner_text="", spent=1,
                            channel="shop_whatsapp", shop="shop-005"))

CASES: tuple[SpeakCase, ...] = tuple(_CASES)
assert len(CASES) == 80, len(CASES)


def run_case(c: SpeakCase) -> tuple[RunResult, ScriptedModel, S.InMemorySpeakStore]:
    shop_id = c.shop if c.channel == "shop_whatsapp" else ""
    send: dict[str, Any] = {"channel": c.channel, "body": c.text, "ends_turn": True}
    if shop_id:
        send["shop_id"] = shop_id
    turns = []
    if c.first:
        turns.append(say("", *c.first))
    if c.repeat:
        # The same update twice in one turn: the second is a duplicate.
        turns.append(say("", *c.alongside, ("send_to_user", dict(send)),
                         ("send_to_user", dict(send))))
    else:
        turns.append(say("", *c.alongside, ("send_to_user", dict(send))))
    # After a hold, the model ends quietly (a held text never leaves).
    turns.append(say("held; nothing further to add"))
    turns.append(say("held; nothing further to add"))
    model = ScriptedModel(turns)
    go = FakeGo(WORLD)
    policy = GoPolicy()
    helper = collector_helper(default_registry(), policy)
    store = S.InMemorySpeakStore()
    day = AT.date()
    # Pre-spend the cap so this turn is the (spent+1)-th unprompted message.
    for _ in range(c.spent):
        aud = S.audience_for(c.channel)
        scope = c.shop if aud in ("shop", "rep") else ""
        store.record_unprompted(TENANT, aud, scope, day)
    h = Harness(book=go, control=go, events=go, gateway=Gateway([model]), policy=policy,
                clock=lambda: AT, speak=S.SpeakConfig(mode="enforce"),
                speak_counts=store, speak_digest=store)
    if c.shop_text:
        ev = RunEvent(event_id=c.cid, tenant_id=TENANT, thread_id=f"t:{c.cid}",
                      wake_source="shop_message", text=c.shop_text, sender="shop",
                      shop_id=c.shop, at=AT)
    elif c.wake:
        sender = "shop" if c.channel == "shop_whatsapp" else "system"
        ev = RunEvent(event_id=c.cid, tenant_id=TENANT, thread_id=f"t:{c.cid}",
                      wake_source=c.wake,  # type: ignore[arg-type]
                      text=c.owner_text, sender=sender,  # type: ignore[arg-type]
                      shop_id=c.shop if sender == "shop" else "", at=AT)
    else:
        ev = RunEvent(event_id=c.cid, tenant_id=TENANT, thread_id=f"t:{c.cid}",
                      wake_source="owner_message", text=c.owner_text, sender="owner", at=AT)
    return h.run(ev, helper), model, store


def judge(c: SpeakCase) -> dict[str, Any]:
    r, _model, store = run_case(c)
    sent = any(d.channel == c.channel and d.kind != "question" for d in r.outbound_drafts)
    decisions = [e for e in r.events if e.type == "speak_decision"]
    reasoned = all(bool(e.short) and bool(e.data.get("action")) for e in decisions)
    on_trace = any(e.type == "run_finished" and e.data.get("speak_decision") for e in r.events)
    dup_sent = (sum(1 for d in r.outbound_drafts if d.channel == c.channel) > 1
                if c.repeat else False)
    dup_held = (any(e.data.get("action") == "stay_quiet"
                    and "already sent" in (e.short or "") for e in decisions)
                if c.repeat else False)
    if c.want == "speak":
        ok = sent and r.status == "done" and reasoned and on_trace and not dup_sent
    elif c.want == "digest":
        ok = (not sent and bool(store.items) and reasoned and on_trace
              and r.status in ("done_quiet", "done"))
    elif c.kind == "duplicate":
        # The first draft says it once; the repeat is held as stay_quiet.
        # Zero duplicate updates: exactly one draft leaves, and the hold is
        # logged with its reason.
        ok = (sum(1 for d in r.outbound_drafts if d.channel == c.channel) == 1
              and dup_held and reasoned and on_trace and not store.items)
    else:
        ok = not sent and not store.items and r.status == "done_quiet" and reasoned
    return {"case": c.cid, "kind": c.kind, "want": c.want, "ok": ok, "sent": sent,
            "status": r.status, "reason": r.reason,
            "decisions": [(e.data.get("action"), e.short) for e in decisions],
            "digest_items": len(store.items), "duplicate_sent": dup_sent}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)
    rows = [judge(c) for c in CASES]
    must = [r for r, c in zip(rows, CASES, strict=True) if c.kind in ("question", "approval", "risk")]
    must_ok = sum(1 for r in must if r["ok"])
    quiet = [r for r, c in zip(rows, CASES, strict=True) if c.want == "quiet"]
    quiet_ok = sum(1 for r in quiet if r["ok"])
    dupes = sum(1 for r in rows if r["duplicate_sent"])
    marks = {
        "speak_recall==100%": must_ok == len(must),
        "quiet_precision>=90%": (quiet_ok / len(quiet) >= 0.9) if quiet else False,
        "zero_duplicate_updates": dupes == 0 and all(r["ok"] for r in rows
                                                     if r["case"].startswith("quiet-dup")),
        "every_decision_has_a_logged_reason": all(
            r["decisions"] and all(a and why for a, why in r["decisions"]) for r in rows),
    }
    by_kind: dict[str, list[int]] = {}
    for c, r in zip(CASES, rows, strict=True):
        b = by_kind.setdefault(c.kind, [0, 0, 0])
        b[1] += 1
        b[0] += r["ok"]
    report = {"generated_by": "python -m mimi.harness.speak_eval",
              "cases": len(rows), "must_speak": f"{must_ok}/{len(must)}",
              "quiet": f"{quiet_ok}/{len(quiet)}", "duplicate_updates": dupes,
              "pass_marks": marks, "passed": passed(marks),
              "by_kind": {k: slice_rate(*v) for k, v in sorted(by_kind.items())},
              "failures": [r for r in rows if not r["ok"]]}
    if not args.no_write:
        EVAL_DIR.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    for r in rows:
        print(f"{'PASS' if r['ok'] else 'FAIL'} {r['case']:<20} want={r['want']:<7} "
              f"sent={r['sent']!s:<5} ({r['status']}) {r['reason'][:70]}")
    print(f"speak: must-speak {must_ok}/{len(must)}, quiet {quiet_ok}/{len(quiet)}, "
          f"duplicates {dupes}; {'PASSED' if report['passed'] else 'FAILED'}")
    return exit_code(marks)


if __name__ == "__main__":
    sys.exit(main())
