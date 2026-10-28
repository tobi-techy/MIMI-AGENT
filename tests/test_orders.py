"""Order intake (RAI-302): words, catalogue matching, prices, credit, the tools
on the real API agent with a scripted model, versions and decided orders.
Offline: no model key, no network."""

from __future__ import annotations

import json
import socket
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from mimi.harness.gateway import ScriptedModel, say
from mimi.harness.inbound import Inbound, Media, Payload
from mimi.harness.policy import ALWAYS_APPROVE, NEVER_TOOLS, GoPolicy
from mimi.harness.router import RouteDecision
from mimi.orders import order as od
from mimi.orders.catalogue import match
from mimi.orders.reading import ListReader, parse
from mimi.orders.tools import ORDER_TOOLS, wanted_in
from mimi.orders.words import fold, numbers_in, pays_now_in, quantities_in
from mimi.store import schema as s
from mimi.store.util import dumps
from mimi.world import load_world

W = load_world()
T = "coral-lane"
LAGOS = ZoneInfo("Africa/Lagos")
T0 = datetime(2026, 10, 26, 17, 0, tzinfo=LAGOS)
UNITS = {x.sku: x.unit for x in W.skus}


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_API_KEY", "")

    def no_network(*a: Any, **k: Any) -> None:
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)


# ------------------------------------------------------------------- words


def test_numbers_in_figures_and_words() -> None:
    assert quantities_in("e ba mi mu paali meta indomie wa") == {3}
    assert quantities_in("Ẹ fi páálí méjì tomato paste") == {2}
    assert quantities_in("bag rice meji (5kg)") == {2}  # 5kg is a size, not a count
    assert quantities_in("two bag rice the five kilo one") == {1, 2}
    assert quantities_in("Sachet milk x6 bags") == {6}
    assert quantities_in("send a carton of soap") == {1}
    assert 5 in numbers_in("5kg") and 5 not in quantities_in("5kg")
    assert fold("1 Litre") == "1l"


def test_pays_now_needs_the_shops_words() -> None:
    assert pays_now_in("send 1 carton spaghetti, I go pay before una deliver")
    assert pays_now_in("I will pay cash when the van comes")
    assert not pays_now_in("I will pay on Friday")
    assert not pays_now_in("send 2 carton indomie")


def test_a_change_asks_for_its_new_number_only() -> None:
    assert wanted_in("make the spaghetti 4 carton instead") == {4}
    assert 4 not in wanted_in("make it 6, not 4")


# --------------------------------------------------------------- catalogue


@pytest.mark.parametrize("words,unit,bought,kind,sku", [
    ("Indomie super pack 2 ctn", "pack", (), "one", "NDL-120-CTN"),
    ("indomie small one", "carton", (), "one", "NDL-70-CTN"),
    ("indomie", "carton", ("NDL-70-CTN",), "one", "NDL-70-CTN"),  # the shop's usual size
    ("indomie", "carton", ("NDL-70-CTN", "NDL-120-CTN"), "several", ""),
    ("6 pack of omo", "pack", (), "several", ""),
    ("the big 900g one omo", "pack", (), "one", "DET-900G"),
    ("3 ctn Tin milk", "ctn", (), "one", "MLK-TIN-CTN"),
    ("Sachet milk x6 bags", "bags", (), "one", "MLK-SCH-BAG"),
    ("3 carton of malt", "carton", (), "unit", ""),  # malt cans come in packs: ask
    ("bag rice meji 5kg", "bag", (), "one", "RICE-5KG"),
    ("igo oil merin", "igo", (), "one", "OIL-1L"),
    ("keg of oil", "keg", (), "one", "OIL-5L"),
    ("pack water bottle mefa", "pack", (), "one", "WATER-75CL"),
    ("pure water", "bag", (), "one", "PWATER-BAG"),
    ("tomato paste", "carton", (), "one", "TPASTE-CTN"),
    ("Toothpaste", "", (), "one", "TPASTE-140"),
    ("Vita-Malt", "", (), "one", "MALTED-400"),
    ("2 bag sugar", "bag", (), "one", "SUG-50KG"),
    ("6 pack sugar", "pack", (), "one", "SUG-1KG"),
    ("toilet roll", "carton", (), "none", ""),
    ("gala", "", (), "none", ""),
])
def test_match(words: str, unit: str, bought: tuple[str, ...], kind: str, sku: str) -> None:
    m = match(words, unit, UNITS, bought)
    assert m.kind == kind and m.sku == sku


def test_prices_totals_and_credit_are_code() -> None:
    noodles = next(x for x in W.skus if x.sku == "NDL-70-CTN")
    ln = od.price_line(noodles, 5, W.live_day, W.price_rise_day)
    assert ln.unit_price_kobo == noodles.price_on(W.live_day, W.price_rise_day)
    assert ln.line_total_kobo == 5 * ln.unit_price_kobo
    merged = od.merge([ln, od.price_line(noodles, 2, W.live_day, W.price_rise_day)])
    assert len(merged) == 1 and merged[0].quantity == 7
    c = od.credit_view(100_000, 30_000, 20_000, 60_000, "credit")
    assert c.available_kobo == 50_000 and c.over_credit and c.over_by_kobo == 10_000
    assert not od.credit_view(0, 50_000, 0, 60_000, "pays_now").over_credit
    q = od.Quote([ln])
    text = od.confirm_text(q, c, "Coral Lane Trading")
    assert "N100" in text and "above your available credit" in text
    assert "credit" in text and "raise" not in text


def test_policy_every_order_waits_for_the_owner() -> None:
    p = GoPolicy()
    for tool in ("submit_order", "revise_order"):
        assert tool in ALWAYS_APPROVE
        assert p.tier(T, tool, {"total_kobo": 1, "payment": "pays_now"}) == "approve"
    assert p.tier(T, "ask_order_question", {}) == "notify"
    assert p.tier(T, "price_order", {}) == "auto"
    assert not set(ORDER_TOOLS) & NEVER_TOOLS


def test_list_reader_parses_and_fails_safe() -> None:
    got = parse(json.dumps({"is_order_list": True, "heading": "Order",
                            "lines": ["5 ctn Indomie (big)", " "], "legibility": 0.9}))
    assert got is not None and got.lines == ("5 ctn Indomie (big)",)
    assert parse("not json") is None
    assert ListReader(None)(b"x").via == "no_model"


# ------------------------------------------------------------ on the agent


@pytest.fixture
def engine() -> Any:
    from mimi.store import ensure_schema, make_engine, seed_world

    e = make_engine("sqlite://")
    ensure_schema(e)
    seed_world(e, W)
    return e


class Run:
    def __init__(self, engine: Any, turns: list[Any]) -> None:
        from mimi.api.agent import Agent

        self.engine = engine
        self.model = ScriptedModel(turns)
        self.agent = Agent(engine, models=[self.model], router_model=None, lease_s=30.0,
                           clock=lambda: T0)
        self.n = 0

    def shop_says(self, shop_id: str, text: str = "", media: Media | None = None,
                  ref: str = "") -> str:
        self.n += 1
        shop = W.shop(shop_id)
        v = self.agent.submit(T, Inbound(
            source="shop_message", org_id=T, external_id=f"t.{shop_id}.{self.n}",
            received_at=T0, payload=Payload(text=text, from_phone=shop.phone,
                                            media=media or Media())), media_ref=ref)
        dec = RouteDecision(status="routed", helper="order", skill="order/order-intake",
                            confidence=1.0, layer="rule", rule="test",
                            thread_id=f"{T}:shop:{shop_id}")
        with self.engine.begin() as conn:
            conn.execute(s.turns.update().where(s.turns.c.turn_id == v.turn_id).values(
                checkpoint=dumps({"decision": dec.model_dump(mode="json"), "steer": {}})))
        for _ in range(8):
            if self.agent.process() is None:
                break
        return v.turn_id

    def orders(self, shop_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(s.approvals).where(
                s.approvals.c.shop_id == shop_id,
                s.approvals.c.tool.in_(("submit_order", "revise_order")))
                .order_by(s.approvals.c.created_ts)).all()
        return [{"id": r.approval_id, "status": r.status, "payload": json.loads(r.payload)}
                for r in rows]

    def out(self, shop_id: str) -> list[Any]:
        with self.engine.connect() as conn:
            return list(conn.execute(sa.select(s.messages).where(
                s.messages.c.shop_id == shop_id, s.messages.c.direction == "out")
                .order_by(s.messages.c.ts)))


def line(as_written: str, item: str, quantity: int | None, unit: str = "") -> dict[str, Any]:
    return {"as_written": as_written, "item": item, "quantity": quantity, "unit": unit}


def test_typed_order_waits_for_approval_and_the_notice_goes_on_approve(engine: Any) -> None:
    r = Run(engine, [say("", ("submit_order", {"shop_id": "shop-011", "lines": [
        line("4 carton of big pack indomie", "big pack indomie", 4, "carton"),
        line("6 carton tomato paste", "tomato paste", 6, "carton")]}))])
    r.shop_says("shop-011", "Please send 4 carton of big pack indomie and 6 carton tomato paste")
    (o,) = r.orders("shop-011")
    p = o["payload"]
    assert o["status"] == "pending" and p["version"] == 1 and not p["over_credit"]
    assert {(x["sku"], x["quantity"]) for x in p["lines"]} == {("NDL-120-CTN", 4),
                                                               ("TPASTE-CTN", 6)}
    assert p["total_kobo"] == sum(x["line_total_kobo"] for x in p["lines"])
    assert p["extends_credit"] is False and p["moves_money"] is False
    msgs = {m.kind: m for m in r.out("shop-011")}
    assert msgs["submit_order_notice"].status == "ready"
    assert msgs["submit_order"].status == "pending_approval"  # the approved notice waits
    calls = r.model.calls
    r.agent.decide(T, o["id"], approve=True, by="owner")
    for _ in range(4):
        if r.agent.process() is None:
            break
    assert r.model.calls == calls  # the approval wake needs no model
    msgs = {m.kind: m for m in r.out("shop-011")}
    assert msgs["submit_order"].status == "ready" and "approved" in msgs["submit_order"].text


def test_a_quantity_or_item_not_in_the_shops_words_is_refused(engine: Any) -> None:
    r = Run(engine, [
        say("", ("submit_order", {"shop_id": "shop-011", "lines": [
            line("4 carton indomie", "indomie", 9, "carton")]})),
        say("", ("submit_order", {"shop_id": "shop-011", "lines": [
            line("4 carton milo", "milo", 4, "carton")]})),
        say("done")])
    r.shop_says("shop-011", "send 4 carton indomie")
    assert r.orders("shop-011") == []


def test_unclear_size_asks_one_question_and_binds_the_answer(engine: Any) -> None:
    r = Run(engine, [
        say("", ("submit_order", {"shop_id": "shop-031", "lines": [
            line("4 carton milk", "milk", 4, "carton")]})),
        say("", ("ask_order_question", {"shop_id": "shop-031"})),
        say("", ("submit_order", {"shop_id": "shop-031", "lines": [
            line("4 carton milk", "milk tin one", 4, "carton")]}))])
    r.shop_says("shop-031", "send 4 carton milk")
    asked = [m for m in r.out("shop-031") if m.kind == "ask_order_question"]
    assert len(asked) == 1 and "Okin Evaporated Milk 160g" in asked[0].text
    assert r.orders("shop-031") == []
    with engine.connect() as conn:
        state = json.loads(conn.execute(sa.select(s.threads.c.state).where(
            s.threads.c.thread_id == f"{T}:shop:shop-031")).scalar())
    assert state["pending_skill"] == "order/order-intake"
    r.shop_says("shop-031", "the tin one")
    (o,) = r.orders("shop-031")
    assert [(x["sku"], x["quantity"]) for x in o["payload"]["lines"]] == [("MLK-TIN-CTN", 4)]


def test_over_credit_is_flagged_never_allowed(engine: Any) -> None:
    r = Run(engine, [say("", ("submit_order", {"shop_id": "shop-008", "lines": [
        line("5 carton of bar soap", "bar soap", 5, "carton")]}))])
    r.shop_says("shop-008", "send 5 carton of bar soap")
    (o,) = r.orders("shop-008")
    p = o["payload"]
    assert o["status"] == "pending" and p["over_credit"] and p["over_by_kobo"] == 50_000
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(s.credit_requests)).scalar() == 0
        assert W.shop("shop-008").credit_limit_kobo == conn.execute(
            sa.select(s.shops.c.credit_limit_kobo).where(s.shops.c.shop_id == "shop-008")).scalar()


def test_pays_now_only_with_the_shops_words(engine: Any) -> None:
    r = Run(engine, [say("", ("submit_order", {"shop_id": "shop-033", "payment": "pays_now",
                                               "lines": [line("1 carton spaghetti", "spaghetti",
                                                              1, "carton")]}))])
    r.shop_says("shop-033", "send 1 carton spaghetti")
    (o,) = r.orders("shop-033")
    assert o["payload"]["payment"] == "credit" and o["payload"]["over_credit"]


def test_repeat_order_is_the_last_delivery_at_todays_prices(engine: Any) -> None:
    from mimi.store.book import SqlGo

    r = Run(engine, [say("", ("submit_order", {"shop_id": "shop-006", "repeat_last": True}))])
    r.shop_says("shop-006", "same as last time please")
    (o,) = r.orders("shop-006")
    last = SqlGo(engine).deliveries(T, "shop-006")[-1]
    assert {(x["sku"], x["quantity"]) for x in o["payload"]["lines"]} == {
        (ln["sku"], ln["quantity"]) for ln in last["lines"]}
    assert all(x["source"] == "repeat" for x in o["payload"]["lines"])


def test_edit_before_approval_supersedes_and_the_old_version_cannot_be_approved(
        engine: Any) -> None:
    from mimi.api.agent import Conflict

    r = Run(engine, [
        say("", ("submit_order", {"shop_id": "shop-019", "lines": [
            line("2 carton spaghetti", "spaghetti", 2, "carton"),
            line("3 bag sachet milk", "sachet milk", 3, "bag")]})),
        say("", ("revise_order", {"shop_id": "shop-019", "changes": [
            {"action": "set", "as_written": "make the spaghetti 4 carton instead",
             "item": "spaghetti", "quantity": 4, "unit": "carton"}]}))])
    r.shop_says("shop-019", "send 2 carton spaghetti and 3 bag sachet milk")
    r.shop_says("shop-019", "make the spaghetti 4 carton instead")
    v1, v2 = r.orders("shop-019")
    assert v1["status"] == "superseded" and v2["status"] == "pending"
    assert v2["payload"]["version"] == 2 and v2["payload"]["order_id"].endswith("-v2")
    assert {(x["sku"], x["quantity"]) for x in v2["payload"]["lines"]} == {
        ("SPAG-CTN", 4), ("MLK-SCH-BAG", 3)}
    with pytest.raises(Conflict):
        r.agent.decide(T, v1["id"], approve=True, by="owner")


def test_a_decided_order_is_never_changed(engine: Any) -> None:
    r = Run(engine, [
        say("", ("submit_order", {"shop_id": "shop-021", "lines": [
            line("2 bag rice 5kg", "rice 5kg", 2, "bag")]})),
        say("", ("revise_order", {"shop_id": "shop-021", "changes": [
            {"action": "set", "as_written": "change the rice to 4 bags", "item": "rice",
             "quantity": 4, "unit": "bags"}]})),
        say("done")])
    r.shop_says("shop-021", "send 2 bag rice 5kg")
    (o,) = r.orders("shop-021")
    r.agent.decide(T, o["id"], approve=True, by="owner")
    r.agent.process()
    r.shop_says("shop-021", "change the rice to 4 bags")
    (after,) = r.orders("shop-021")
    assert after["status"] == "approved" and after["payload"] == o["payload"]


def test_list_photo_lines_are_the_shops_words(engine: Any, tmp_path: Any) -> None:
    import hashlib

    from mimi.api.agent import Agent
    from mimi.api.helpers import HelperDeps
    from mimi.api.media import LocalMedia
    from mimi.models.base import ChatResult

    class Vision:
        name = "vision-stub"

        def complete(self, messages: Any, **k: Any) -> ChatResult:
            return ChatResult(json.dumps({"is_order_list": True, "heading": "Order",
                                          "lines": ["5 ctn Indomie (big)", "3 ctn Tin milk"],
                                          "legibility": 0.9}), "vision-stub")

    data = b"\xff\xd8 synthetic list photo"
    (tmp_path / "list.jpg").write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    pid = "ph_" + sha[:12]
    model = ScriptedModel([
        say("", ("read_order_photo", {"shop_id": "shop-013", "photo_id": pid})),
        say("", ("submit_order", {"shop_id": "shop-013", "lines": [
            line("5 ctn Indomie (big)", "Indomie big", 5, "ctn"),
            line("3 ctn Tin milk", "Tin milk", 3, "ctn")]}))])
    r = Run(engine, [])
    r.model = model
    r.agent = Agent(engine, models=[model], router_model=None, lease_s=30.0, clock=lambda: T0,
                    deps=HelperDeps(vision=Vision(), media=LocalMedia(tmp_path)))
    r.shop_says("shop-013", media=Media(kind="image", mime="image/jpeg", sha256=sha,
                                        width=1000, height=1333, camera_exif=True,
                                        caption="my order"), ref="local:list.jpg")
    (o,) = r.orders("shop-013")
    assert {(x["sku"], x["quantity"]) for x in o["payload"]["lines"]} == {
        ("NDL-120-CTN", 5), ("MLK-TIN-CTN", 3)}


def test_remove_a_line_when_the_quote_names_two_products(engine: Any) -> None:
    r = Run(engine, [
        say("", ("submit_order", {"shop_id": "shop-037", "lines": [
            line("3 carton indomie", "indomie", 3, "carton"),
            line("5 tin sardine", "sardine", 5, "tin")]})),
        say("", ("revise_order", {"shop_id": "shop-037", "changes": [
            {"action": "remove", "as_written": "abeg remove the sardine, only the indomie",
             "item": "tin sardine", "quantity": None, "unit": "tin"}]}))])
    r.shop_says("shop-037", "bring 3 carton indomie and 5 tin sardine")
    r.shop_says("shop-037", "abeg remove the sardine, only the indomie")
    v1, v2 = r.orders("shop-037")
    assert v1["status"] == "superseded"
    assert [(x["sku"], x["quantity"]) for x in v2["payload"]["lines"]] == [("NDL-120-CTN", 3)]
