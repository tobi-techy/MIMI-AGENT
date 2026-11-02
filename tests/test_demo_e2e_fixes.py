"""Fixes from the live distributor test of the demo API (30 Oct 2026).

1. Owner order and stock questions route by rule (never Brand, never "ask
   your rep"); Brand and the receipt Order helper end quietly and Mimi takes
   the message back.
2. The morning brief is answered by code from the book with a list card.
3. One overdue total on every surface (Collector dropped the shops past its
   ``limit``: N7,154,850 against the book's N7,231,000).
4. The demo visitor address: never X-Forwarded-For; CF-Connecting-IP only
   from a trusted peer.
5. The per-visitor cap defaults to N400 a day and a refusal has one shape.
6. Naira as the owner reads it: ``₦``.

Offline: scripted models and a SQLite file per test; no model is paid.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mimi import owed
from mimi.api import cards as cx
from mimi.api import demo, reads
from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.api.demo import DemoConfig, DemoRefused, client_ip
from mimi.brand.tools import BRAND_SKILL, BRAND_SKILL_RECORDED
from mimi.flags import FIXES_ENV
from mimi.harness import claims as C
from mimi.harness.collector import InvoicesIn, get_invoices
from mimi.harness.gateway import ScriptedModel
from mimi.harness.router import owner_rule
from mimi.harness.router_eval import OWNER_PHONE
from mimi.receipts.tools import ORDER_SKILL
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.store.book import SqlGo
from mimi.world.loader import load_world
from tests.test_handoffs import CHAN_KEY, OWNER_KEY, make_script, owner_replies

A = "coral-lane"
W = load_world()
BOOK_OVERDUE_KOBO = 723_100_000  # N7,231,000: the demo shops' overdue, summed by hand


@pytest.fixture()
def env(store_url: str) -> Iterator[dict[str, Any]]:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    agent = Agent(engine, models=[ScriptedModel(make_script())], worker_id="e2e-fix-test")
    keys = parse_keys(f"{A}:owner:{OWNER_KEY},{A}:channel:{CHAN_KEY}")
    app = create_app(Settings(keys=keys, wait_s=5), agent, workers=0)
    with TestClient(app) as c:
        yield {"c": c, "agent": agent, "engine": engine}
    engine.dispose()


def owner_turn(c: TestClient, mid: str, text: str) -> dict[str, Any]:
    r = c.post("/v1/turns", json={"message_id": mid, "source": "owner_message",
                                  "phone": OWNER_PHONE, "text": text},
               headers={"Authorization": f"Bearer {CHAN_KEY}"})
    assert r.status_code == 200, r.text
    return dict(r.json())


def figures(text: str) -> list[int]:
    """Every naira figure in ``text`` as kobo."""
    out = []
    for m in re.finditer(r"[N₦](\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?", text):
        out.append(int(m.group(1).replace(",", "")) * 100 + int(m.group(2) or 0))
    return out


class _Grants:
    def allows(self, shop_id: str, scope: str) -> bool:
        return True


class _Ctx:
    def __init__(self, go: SqlGo) -> None:
        self.book, self.tenant_id, self.grants = go, A, _Grants()


# ---------------------------------------------------- 3. one overdue total


def test_the_book_overdue_is_the_hand_sum(env: dict[str, Any]) -> None:
    rows = env["agent"].go.receivables(A, None)
    book = owed.totals(rows)
    assert book.overdue_kobo == BOOK_OVERDUE_KOBO
    assert len(book.debtors()) == 24  # more than Collector's default limit of 20
    # The three definitions the code had agree on this book.
    assert book.overdue_kobo == sum(r["outstanding_kobo"] for r in rows
                                    if r["status"] == "overdue")


@pytest.fixture()
def fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FIXES_ENV, "1")


@pytest.mark.parametrize("limit", [20, 5, 1, 50])
def test_collector_total_is_every_shop_not_the_page_it_lists(env: dict[str, Any], fixed: None,
                                                              limit: int) -> None:
    out = get_invoices(_Ctx(env["agent"].go), InvoicesIn(limit=limit))  # type: ignore[arg-type]
    assert len(out.shops) == min(limit, 24)
    # Before the fix: limit 20 gave N7,154,850 (4 shops, N76,150, dropped).
    assert out.total_overdue_kobo == BOOK_OVERDUE_KOBO


def test_without_the_switch_collector_replays_as_recorded(env: dict[str, Any],
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    # Off (CI's recorded replays): the old page total, until the re-record.
    monkeypatch.delenv(FIXES_ENV, raising=False)
    out = get_invoices(_Ctx(env["agent"].go), InvoicesIn(limit=20))  # type: ignore[arg-type]
    assert out.total_overdue_kobo == 715_485_000


def test_the_dockerfile_turns_the_switch_on() -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert f"{FIXES_ENV}=1" in text


def test_collector_floor_still_applies_to_the_total(env: dict[str, Any], fixed: None) -> None:
    out = get_invoices(_Ctx(env["agent"].go),  # type: ignore[arg-type]
                       InvoicesIn(min_overdue_naira=500_000, limit=1))
    want = sum(s.overdue_kobo for s in owed.totals(env["agent"].go.receivables(A, None)
                                                   ).debtors() if s.overdue_kobo > 50_000_000)
    assert out.total_overdue_kobo == want and len(out.shops) == 1


def test_every_surface_shows_the_same_overdue_total(env: dict[str, Any]) -> None:
    c, engine = env["c"], env["engine"]
    shops = c.get("/v1/shops", headers={"Authorization": f"Bearer {OWNER_KEY}"}).json()
    assert shops["overdue_total_kobo"] == BOOK_OVERDUE_KOBO
    assert shops["overdue_total_naira"] == "₦7,231,000"
    assert sum(s["overdue_kobo"] for s in shops["shops"]) == BOOK_OVERDUE_KOBO
    assert reads.list_shops(engine, A)["overdue_total_kobo"] == BOOK_OVERDUE_KOBO
    debtors = cx._debtors(env["agent"].go, A)
    assert sum(r.overdue_kobo for r in debtors) == BOOK_OVERDUE_KOBO
    assert cx.brief_facts(engine, env["agent"].go, A)["overdue_kobo"] == BOOK_OVERDUE_KOBO


def test_who_owes_the_most_carries_the_book_total(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "fix.owes.1", "who owes me the most?")
    assert t["skill"] == "collector/chase"
    (text,) = owner_replies(t)
    assert "₦7,231,000" in text and "N7,154,850" not in text and "₦7,154,850" not in text
    assert t["card"]["kind"] == "shop_row"


def test_a_top_five_ask_gets_a_list_of_five_with_the_book_total(env: dict[str, Any]) -> None:
    ans = cx.debtor_answer(env["agent"].go, A, "collector/chase",
                           "Who owes me the most right now? Top 5 with amounts and how late.",
                           "2026-10-26")
    assert ans.card is not None and ans.card.kind == "list" and len(ans.card.items) == 5
    assert ans.card.total_kobo == BOOK_OVERDUE_KOBO
    assert [i.amount_kobo for i in ans.card.items] == sorted(
        (i.amount_kobo or 0 for i in ans.card.items), reverse=True)
    assert "₦7,231,000" in ans.sentence


# ------------------------------------------------------- 2. morning brief


BRIEF_ASKS = [
    "Good morning Mimi. Give me my morning brief: who owes, what came in yesterday, "
    "what needs me today.",
    "Morning Mimi. This is Silver Eagle General Concern, we supply 125+ supermarket and mall "
    "stores. What needs my attention today?",
    "Good morning Mimi. I run Ravenna Nigeria Ltd, sole distributor of Illera Oil in Lagos "
    "and Ogun, open market and modern trade. Give me my morning brief.",
]


@pytest.mark.parametrize("text", BRIEF_ASKS)
def test_the_brief_asks_route_by_rule(text: str) -> None:
    assert owner_rule(text) == "mimi/owner-brief"


def test_the_morning_brief_is_answered_from_the_book(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "fix.brief.1", BRIEF_ASKS[0])
    assert t["skill"] == "mimi/owner-brief" and t["status"] == "done"
    (text,) = owner_replies(t)
    assert "not ready" not in text.lower()
    facts = cx.brief_facts(env["engine"], env["agent"].go, A)
    # Every figure in the reply is one code read from the book.
    allowed = {facts["overdue_kobo"], facts["paid_kobo"]} | {
        r.overdue_kobo for r in facts["top"]}
    assert figures(text) and set(figures(text)) <= allowed
    assert "₦7,231,000" in text and "N7" not in text
    card = t["card"]
    assert card["kind"] == "list" and card["skill"] == "mimi/owner-brief"
    ids = [i["item_id"] for i in card["items"]]
    assert ids[0] == "owes:total" and "in:payments" in ids and "needs:approvals" in ids
    assert card["items"][0]["amount_kobo"] == BOOK_OVERDUE_KOBO
    # No model was called for it (spend 0).
    assert t["handoff"]["to_helper"] == "mimi"
    assert 2 <= len(t["chips"]) <= 3


def test_brief_counts_match_the_book(env: dict[str, Any]) -> None:
    engine, go = env["engine"], env["agent"].go
    f = cx.brief_facts(engine, go, A)
    assert f["as_of"] == "2026-10-26" and f["yesterday"] == "2026-10-25"
    with engine.connect() as conn:
        rows = conn.execute(sa.select(s.payments).where(
            s.payments.c.tenant_id == A, s.payments.c.paid_on == "2026-10-25",
            s.payments.c.fake_alert == sa.false())).all()
    assert f["paid_n"] == len(rows) and f["paid_kobo"] == sum(int(r.amount_kobo) for r in rows)
    assert f["disputes_n"] == 2 and f["approvals_n"] == 0
    assert f["low_items_n"] == len(cx.low_stock(go, A))


# ------------------------------------------------ 1. orders and stock routing


@pytest.mark.parametrize("text,gold", [
    ("Any orders available?", "order/open-orders"),
    ("What's running out?", "stock/low-stock-report"),
    ("What's running out at Mama Sola Provisions?", "stock/low-stock-report"),
    ("which items are running low", "stock/low-stock-report"),
    ("anything out of stock?", "stock/low-stock-report"),
])
def test_owner_orders_and_stock_route_by_rule(text: str, gold: str) -> None:
    assert owner_rule(text) == gold


def test_any_orders_is_answered_by_order_without_a_rep(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "fix.ord.1", "Any orders available?")
    assert t["skill"] == "order/open-orders" and t["handoff"]["to_helper"] == "order"
    for text in owner_replies(t):
        assert not C.deflections(text) and "rep" not in text.lower()


def test_running_out_at_one_shop_lists_only_that_shop(env: dict[str, Any]) -> None:
    ans = cx.stock_answer(env["agent"].go, A, "2026-10-26",
                          "What's running out at Mama Sola Provisions?")
    assert ans.card is not None and "Mama Sola Provisions" in ans.card.title
    assert all(i.shop_id == "shop-001" for i in ans.card.items)


def test_brand_and_order_prompts_never_send_people_to_a_rep() -> None:
    for prompt in (BRAND_SKILL, ORDER_SKILL):
        assert "rep will help" not in prompt and "right helper or the rep" not in prompt
        assert "send nothing and end the turn" in prompt


def test_the_switch_picks_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimi.api.helpers import build_helpers

    go = SqlGo(make_engine("sqlite://"))
    monkeypatch.setenv(FIXES_ENV, "1")
    on = build_helpers(go=go)
    assert on["brand"].skill == BRAND_SKILL and on["brand"].skill_version == "brand-v0.2"
    assert "rep will help" not in on["order"].skill
    monkeypatch.delenv(FIXES_ENV)
    off = build_helpers(go=go)
    assert off["brand"].skill == BRAND_SKILL_RECORDED
    assert off["brand"].skill_version == "brand-v0.1"
    assert off["order"].skill_version == "order-v0.1"


def test_a_quiet_brand_hands_back_to_mimi(store_url: str, fixed: None) -> None:
    """Brand with nothing to say (its new prompt) is a hand-back, not a dead end."""
    from mimi.harness.gateway import ModelTurn, say

    base = make_script()

    def script(messages: Any, tools: Any) -> ModelTurn:
        system = next((str(m["content"]) for m in messages if m["role"] == "system"), "")
        if "You are Brand" in system:
            return say("")
        return base(messages, tools)  # type: ignore[no-any-return]

    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    agent = Agent(engine, models=[ScriptedModel(script)], worker_id="quiet-brand")
    keys = parse_keys(f"{A}:owner:{OWNER_KEY},{A}:channel:{CHAN_KEY}")
    app = create_app(Settings(keys=keys, wait_s=5), agent, workers=0)
    with TestClient(app) as c:
        r = c.post("/v1/turns", json={"message_id": "fix.qb.1", "source": "owner_message",
                                      "phone": OWNER_PHONE, "text": "Any orders available now",
                                      "assign_to": "brand"},
                   headers={"Authorization": f"Bearer {CHAN_KEY}"})
    engine.dispose()
    t = r.json()
    assert t["handback"]["from_helper"] == "brand" and t["handback"]["code"] == "no_tools"
    assert t["skill"] == "order/open-orders" and owner_replies(t)


# ------------------------------------------------------ 4. visitor address


CONF = DemoConfig()


@pytest.mark.parametrize("peer,headers,want", [
    # A client on the open internet: its own headers are never believed.
    ("198.51.100.7", {"x-forwarded-for": "1.2.3.4"}, ("198.51.100.7", "peer")),
    ("198.51.100.7", {"cf-connecting-ip": "1.2.3.4"}, ("198.51.100.7", "peer")),
    # Through the platform edge (private peer): Cloudflare's header.
    ("10.1.2.3", {"cf-connecting-ip": "203.0.113.5"}, ("203.0.113.5", "header")),
    ("10.1.2.3", {"cf-connecting-ip": "203.0.113.5", "x-forwarded-for": "9.9.9.9"},
     ("203.0.113.5", "header")),
    # XFF alone through the edge is still not believed.
    ("10.1.2.3", {"x-forwarded-for": "9.9.9.9"}, ("10.1.2.3", "peer")),
    # Garbage in the header falls back to the peer.
    ("10.1.2.3", {"cf-connecting-ip": "not-an-ip"}, ("10.1.2.3", "peer")),
    ("::1", {"cf-connecting-ip": "2001:db8::1"}, ("2001:db8::1", "header")),
])
def test_client_ip(peer: str, headers: dict[str, str], want: tuple[str, str]) -> None:
    assert client_ip(peer, headers, CONF) == want


def test_trust_any_peer_and_header_off() -> None:
    star = DemoConfig(trusted_proxies=("*",))
    assert client_ip("198.51.100.7", {"cf-connecting-ip": "203.0.113.5"}, star) == (
        "203.0.113.5", "header")
    off = DemoConfig(client_ip_header="", trusted_proxies=("*",))
    assert client_ip("10.0.0.1", {"cf-connecting-ip": "203.0.113.5"}, off) == (
        "10.0.0.1", "peer")


# ------------------------------------------------ 5. caps and the refusal


def test_default_caps() -> None:
    d = DemoConfig.from_env({})
    assert d.ip_daily_cap_kobo == 40_000 and d.global_daily_cap_kobo == 200_000


def test_a_cap_refusal_has_one_shape_the_console_reads(store_url: str) -> None:
    from tests.test_api_demo import cfg, make, shop_turn, spend, start

    client, engine = make(store_url, cfg(ip_daily_cap_kobo=40_000, global_daily_cap_kobo=200_000))
    with client as c:
        sess = start(c)
        spend(engine, sess["tenant_id"], 40_000)
        r = shop_turn(c, sess, "fix.cap.1")
        me = c.get("/v1/me", headers={"Authorization": f"Bearer {sess['token']}",
                                      "CF-Connecting-IP": "198.51.100.1"}).json()
    engine.dispose()
    assert r.status_code == 429
    body = r.json()
    assert body["detail"] == demo.IP_CAPPED
    lim = body["demo_limit"]
    assert lim["code"] == "visitor_daily_cap" and lim["scope"] == "visitor"
    assert lim["spent_kobo"] == 40_000 and lim["cap_kobo"] == 40_000
    assert lim["cap_naira"] == "₦400" and lim["retry"] == "tomorrow"
    assert lim["resets_at"].endswith("+01:00") and "T00:00:00" in lim["resets_at"]
    assert lim["message"][0].isupper() and lim["title"]
    assert set(lim) == {"code", "scope", "title", "message", "spent_kobo", "cap_kobo",
                        "spent_naira", "cap_naira", "resets_at", "retry"}
    assert me["demo"]["ip_source"] == "header" and me["demo"]["resets_at"]


def test_every_refusal_payload_has_the_same_keys() -> None:
    keys = None
    for exc in (DemoRefused(503, demo.PAUSED, code="paused"),
                DemoRefused(429, demo.GLOBAL_CAPPED, code="global_daily_cap", scope="global",
                            spent_kobo=1, cap_kobo=2)):
        p = exc.payload()
        assert set(p) == {"detail", "demo_limit"}
        keys = keys or set(p["demo_limit"])
        assert set(p["demo_limit"]) == keys


# ------------------------------------------------------------- 6. naira


@pytest.mark.parametrize("raw,want", [
    ("Total overdue: N7,231,000.", "Total overdue: ₦7,231,000."),
    ("N500 and N 1,200.50", "₦500 and ₦1,200.50"),
    ("No, INV-N12 stays", "No, INV-N12 stays"),
    ("Already ₦100", "Already ₦100"),
])
def test_naira_sign(raw: str, want: str) -> None:
    assert owed.naira_sign(raw) == want


def test_owner_replies_and_reads_use_the_naira_sign(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "fix.n.1", "Who is the highest debtor?")
    for text in owner_replies(t):
        assert not re.search(r"(?<![A-Za-z])N\d", text), text
    assert t["card"]["shop_row"]["owed_naira"].startswith("₦")
    assert owed.naira(723_100_000) == "₦7,231,000" and owed.naira(150) == "₦1.50"
