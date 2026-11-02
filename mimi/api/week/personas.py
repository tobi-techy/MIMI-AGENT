"""The week's scenes and the shops' and owner's words: seeded, deterministic, ₦0.

The shops' words come from fixed, seeded templates in the short casual
Nigerian English (and some Pidgin) the world's chat traits describe, so the
offline week costs nothing and gives the same week every run. The model-driven
persona (``mimi.sim.persona``) stays the live day's simulator.

Every figure a shop states is computed in code from the book (a cash amount is
a round share of what the shop owes); the screenshots and shelf photos are the
synthetic fixtures under ``eval/`` (``eval/proof/screens.jsonl``,
``eval/credit/shelves.jsonl``), each sent by the shop it was drawn for.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from functools import cache
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from mimi.api.e2e import scenes as e2e
from mimi.store import schema as s
from mimi.tz import LAGOS

if TYPE_CHECKING:
    from mimi.api.week.runner import Sent, Week

EVAL = e2e.ROOT / "eval"
TAP = "btn:stock/stock-count"

# Shelf photos for the credit asks: (shop, fixture), each drawn for that shop.
CREDIT_ASKS: tuple[tuple[str, str], ...] = (
    ("shop-020", "x01"), ("shop-011", "s11"), ("shop-019", "s12"), ("shop-024", "s13"),
    ("shop-031", "s14"), ("shop-038", "s15"), ("shop-021", "s17"), ("shop-026", "s18"))
# Fakes whose only tell is another shop's earlier proof with the same reference
# (the proof eval seeds that prior; the week has no image for it), so they are
# left out rather than counted as misses the week cannot set up.
CONTEXT_FAKES = frozenset({"f06", "f07"})
CREDIT_TEXT = ("Abeg increase my credit, business is moving and I want to carry more goods",
               "Please can you raise my credit limit? I need more stock this week",
               "Good afternoon. I want more credit so I can take bigger order")
YES_TEXT = ("YES", "Yes", "yes all complete", "Yes o, everything complete. Thanks",
            "yes received")
UNCLEAR_TEXT = ("ok I go check am later", "seen", "ok noted")
SMALL_TALK = ("Good morning", "Good afternoon o", "Thank you, God bless")
BALANCE_TEXT = ("How much I owe una now?", "Abeg wetin be my balance?",
                "Please how much is my balance")
PROMISE_TEXT = ("I go pay on Friday", "I will pay the balance on Saturday",
                "Money go enter Friday, I go pay")
MONEY_ASK_TEXT = ("Abeg refund the 5k I overpaid last week to my account",
                  "Please transfer my money back, I paid twice")
OWNER_TEXT: dict[int, tuple[tuple[str, str], ...]] = {
    0: (("brief", "Good evening. How is business today?"),),
    1: (("owes", "Who owes me the most this week?"),),
    2: (("owes", "Which shops owe me the most?"), ("brief", "How did we do today?")),
    3: (("credit", "Should I give Blessed Funke Mini Mart more credit?"),),
    4: (("owes", "Who owes me the most?"), ("low", "Which shops are running low?")),
    5: (("brief", "How is business this week?"),),
    6: (("owes", "Who owes me the most going into next week?"),),
}


@cache
def screens() -> dict[str, dict[str, Any]]:
    rows = [json.loads(x) for x in (EVAL / "proof" / "screens.jsonl").read_text().splitlines()]
    return {r["id"]: r for r in rows}


def media(img: e2e.Image, caption: str, *, camera: bool) -> dict[str, Any]:
    return {"kind": "image", "ref": img.ref, "mime": "image/jpeg", "sha256": img.sha256,
            "width": img.width, "height": img.height, "camera_exif": camera,
            "caption": caption}


def _pick(w: Week, xs: tuple[str, ...]) -> str:
    return xs[w.rng.randrange(len(xs))]


def _owed(w: Week, sid: str) -> int:
    with w.engine.connect() as conn:
        return int(conn.execute(sa.select(sa.func.sum(s.receivables.c.outstanding_kobo)).where(
            s.receivables.c.tenant_id == w.tenant, s.receivables.c.shop_id == sid)).scalar()
                   or 0)


def _replies(rec: Sent) -> list[dict[str, Any]]:
    return [r for r in rec.view.get("replies", []) if r.get("channel") == "shop_whatsapp"]


def _kinds(rec: Sent) -> set[str]:
    return {str(r.get("kind", "")) for r in _replies(rec)}


# ------------------------------------------------------------------ receipts


def delivery(w: Week, sid: str, did: str, when: datetime, shortage: Any = None,
             before: Any = None) -> None:
    def react(rec: Sent) -> None:
        if "send_delivery_receipt" not in _kinds(rec):
            return
        later = when + timedelta(minutes=20 + w.rng.randrange(90))
        roll = w.rng.random()
        if shortage is not None:
            text = (f"I no see the {shortage.missing} {shortage.unit} of {shortage.name}, "
                    "e short")
            w.shop_turn("receipt", sid, "dispute", later, text=text)
        elif roll < 0.8:
            w.shop_turn("receipt", sid, "yes", later, text=_pick(w, YES_TEXT))
        else:
            w.shop_turn("receipt", sid, "unclear", later, text=_pick(w, UNCLEAR_TEXT),
                        react=again)

    def again(rec: Sent) -> None:
        if "ask_receipt_again" in _kinds(rec):
            w.shop_turn("receipt", sid, "yes2", datetime.fromisoformat(rec.sim_at)
                        + timedelta(minutes=15), text="yes all complete")

    w.event("receipt", "webhook", "delivery_completed", sid, when, f"dlv.{did}", react=react,
            before=before)


# ------------------------------------------------------------------ stock


def _asked_sku(w: Week, sid: str) -> str:
    with w.engine.connect() as conn:
        rows = conn.execute(sa.select(s.intents.c.payload).where(
            s.intents.c.tenant_id == w.tenant, s.intents.c.shop_id == sid,
            s.intents.c.tool == "ask_stock_count").order_by(s.intents.c.created_at)).all()
    return str(json.loads(rows[-1].payload)["sku"]) if rows else ""


@cache
def _truth() -> Any:
    from mimi.stock.truth import generate_truth
    from mimi.world.loader import load_world

    return generate_truth(load_world()).items


def _left(sid: str, sku: str, day: int) -> int:
    t = _truth().get((sid, sku))
    if t is None:
        return 0
    try:
        return int(t.counted(day))
    except (IndexError, KeyError):
        return int(t.counted(len(t.start_stock) - 1))


def stock_check(w: Week, sid: str, when: datetime) -> None:
    def react(rec: Sent, n: int = 0) -> None:
        if "ask_stock_count" not in _kinds(rec):
            return
        sku = _asked_sku(w, sid)
        left = _left(sid, sku, w.day)
        bucket = "0-2" if left <= 2 else "3-5" if left <= 5 else "6+"
        later = datetime.fromisoformat(rec.sim_at) + timedelta(minutes=30 + w.rng.randrange(60))
        roll = w.rng.random()
        if n == 0 and roll < 0.12:
            w.shop_turn("stock", sid, "unclear", later, text="I never check yet",
                        react=lambda r: react(r, 1))
        elif roll < 0.75 or n:
            w.shop_turn("stock", sid, f"tap{n}", later, text=bucket, button_id=TAP)
        else:
            w.shop_turn("stock", sid, "words", later, text=f"e remain {left} only")

    w.event("stock", "routine", "stock.weekly_check", sid, when, "weekly", react=react)


# ------------------------------------------------------------------ credit


def credit_ask(w: Week, sid: str, fixture: str, when: datetime) -> None:
    def react(rec: Sent) -> None:
        if "ask_for_shelf_photo" not in _kinds(rec):
            return
        img = e2e.shelf(fixture)
        w.shop_turn("credit", sid, "photo", datetime.fromisoformat(rec.sim_at)
                    + timedelta(minutes=25), media=media(img, "this is my shelf", camera=True),
                    react=photo_again)

    def photo_again(rec: Sent) -> None:
        if "ask_for_shelf_photo" in _kinds(rec):  # one ask again: the same photo comes back
            img = e2e.shelf(fixture)
            w.shop_turn("credit", sid, "photo2", datetime.fromisoformat(rec.sim_at)
                        + timedelta(minutes=20), media=media(img, "see am again", camera=True))

    w.shop_turn("credit", sid, "ask", when, text=_pick(w, CREDIT_TEXT), react=react)


# ------------------------------------------------------------------ payments


def screenshot(w: Week, sid_: str, fixture: str, label: str, when: datetime | None = None,
               path: str = "") -> None:
    row = screens()[fixture]
    img = e2e.screenshot(fixture)
    at = when or datetime.fromisoformat(str(row["received_at"])).astimezone(LAGOS)
    cap = {"genuine": "I don pay, see the alert", "fake": "I have paid, check it",
           }.get(str(row["category"]), "payment for today")
    w.shop_turn("payment", str(row["shop_id"]), label, at,
                media=media(img, cap, camera=False), path=path or str(row["category"]))


def bank_credit(w: Week, fixture: str, when: datetime) -> None:
    row = screens()[fixture]
    sid = str(row["shop_id"])
    f = row["expected_fields"]

    def go() -> None:
        ref = str(row["truth"].get("reference") or "")
        w.go.bank_credit(sid, int(f["amount_kobo"]), w.day, when.strftime("%Y-%m-%dT%H:%M:%S"),
                         f"NIP/{str(f['bank']).upper()}/{f['sender']}/{ref}")

    w.event("payment", "webhook", "bank_credit", sid, when, f"credit.{fixture}",
            path="bank_credit", before=go)


def asks_if_landed(w: Week, fixture: str, when: datetime) -> None:
    sid = str(screens()[fixture]["shop_id"])
    w.shop_turn("payment", sid, "landed", when, text="I don pay the transfer, una see am?",
                path="bank_credit")


def cash(w: Week, sid: str, when: datetime) -> None:
    """The rep logs cash (Go), then the shop says so. A round N5,000 share of
    what the shop owes, at least N5,000."""
    owed = _owed(w, sid)
    amount = max(500_000, (owed // 2) // 500_000 * 500_000)
    day = w.day

    def go() -> None:
        w.go.cash_to_rep(sid, amount, day)

    w.shop_turn("payment", sid, "cash", when,
                text=f"I don give your rep {amount // 100_000}k cash this morning",
                path="cash_to_rep", before=go)


# ------------------------------------------------------------------ the days


def plan_day(w: Week, n: int) -> None:
    """Queue day ``n`` (0 = Monday) of the week."""
    day = w.day
    rng = w.rng
    shop_ids = sorted(w.shops)
    if n == 0:
        for sid in sorted(rng.sample(shop_ids, 12)):
            stock_check(w, sid, w.at(day, 7, 30))
        for d in w.world.live_deliveries:
            delivery(w, d.shop_id, d.delivery_id, w.at(day, d.minute // 60, d.minute % 60),
                     d.shortage)
        for fid, row in sorted(screens().items()):
            if fid in CONTEXT_FAKES:
                continue
            screenshot(w, str(row["shop_id"]), fid, f"shot.{fid}")
    else:
        if n == 3:
            for sid in sorted(rng.sample(shop_ids, 6)):
                stock_check(w, sid, w.at(day, 7, 30))
        if n <= 5:  # top-ups Tuesday to Saturday, no Sunday van
            for sid in sorted(rng.sample(shop_ids, 6 if n < 5 else 4)):
                minute = rng.randrange(9 * 60, 16 * 60)
                did = w.go.next_delivery_id(day)
                lines_rng = random.Random(f"{w.run.seed}:{w.loop}:{did}")

                def log(sid: str = sid, minute: int = minute, did: str = did,
                        r: random.Random = lines_rng) -> None:
                    w.go.add_delivery(sid, day, minute, r, delivery_id=did)  # Go, at drop time

                delivery(w, sid, did, w.at(day, minute // 60, minute % 60), before=log)
    genuine = sorted(f for f, r in screens().items() if r["category"] == "genuine")
    if n == 1:  # the bank feed brings Monday's genuine transfers
        for i, fid in enumerate(genuine):
            bank_credit(w, fid, w.at(day, 9, 0) + timedelta(minutes=3 * i))
        for fid in sorted(rng.sample(genuine, 4)):
            asks_if_landed(w, fid, w.at(day, 11, rng.randrange(60)))
    if n == 2:  # a shop resends a screenshot it already sent
        for fid in sorted(rng.sample(genuine, 4)):
            screenshot(w, str(screens()[fid]["shop_id"]), fid, f"resend.{fid}",
                       w.at(day, 10, rng.randrange(60)), path="resend")
    if n in (2, 3):
        for sid, fix in CREDIT_ASKS[(n - 2) * 4:(n - 1) * 4]:
            credit_ask(w, sid, fix, w.at(day, 12, rng.randrange(60)))
    if 1 <= n <= 5:
        for sid in sorted(rng.sample(shop_ids, 2)):
            cash(w, sid, w.at(day, 13, rng.randrange(60)))
        for sid in sorted(rng.sample(shop_ids, 2)):
            w.shop_turn("payment", sid, "promise", w.at(day, 14, rng.randrange(60)),
                        text=_pick(w, PROMISE_TEXT), path="promise")
    for sid in sorted(rng.sample(shop_ids, 3)):
        w.shop_turn("balance", sid, "ask", w.at(day, 15, rng.randrange(60)),
                    text=_pick(w, BALANCE_TEXT))
    sid = shop_ids[rng.randrange(len(shop_ids))]
    w.shop_turn("money_ask", sid, "refund", w.at(day, 16, rng.randrange(60)),
                text=_pick(w, MONEY_ASK_TEXT))
    for sid in sorted(rng.sample(shop_ids, 2)):
        w.shop_turn("chat", sid, "hello", w.at(day, 8, rng.randrange(60)),
                    text=_pick(w, SMALL_TALK))
    for i, (label, text) in enumerate(OWNER_TEXT.get(n, ())):
        w.owner_turn(label, text, w.at(day, 18 + i, 30))
    w.owner_decides(w.at(day, 20, 30))


def decision(w: Week, a: dict[str, Any]) -> str:
    """The owner's call on one approvals item ("" leaves it waiting)."""
    tool = str(a.get("tool", ""))
    roll = w.rng.random()
    if tool in ("draft_restock", "revise_restock"):
        return "approve" if roll < 0.7 else "reject"
    if tool == "submit_credit_review":
        return "approve" if roll < 0.5 else "reject"
    if tool in ("submit_order", "revise_order", "draft_brand_email"):
        return "approve"
    if tool == "raise_daily_allowance":
        # Offline the owner raises Mimi's allowance; a live run never does (the
        # naira ceiling is the hard stop).
        return "" if w.ceiling is not None else "approve"
    return ""  # fake-alert flags and the rest: the owner checks with the bank first
