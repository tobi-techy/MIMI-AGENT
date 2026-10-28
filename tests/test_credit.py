"""Credit (RAI-271): SAM client, photo checks, counting, the recommendation,
the verifier, the tools on the harness and the scene, offline."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from mimi.credit import copy
from mimi.credit.book import CreditGo
from mimi.credit.eval import CONVOS, judge_convo, load_cases, never_suite, run_convo
from mimi.credit.looks import LOOKS, item_name, pieces_per_unit
from mimi.credit.photo import check_photo
from mimi.credit.render import Run, ShelfSpec, render
from mimi.credit.review import CreditFile, about_kobo, credit_file, recommend
from mimi.credit.sam import (
    Box,
    CachedSegmenter,
    MetaSam,
    SamCacheMiss,
    SamError,
    Segmentation,
    parse_boxes,
    sam_cost_kobo,
)
from mimi.credit.scene import CreditScene, offline_router, scripted_model
from mimi.credit.shelf import count_shelf, keep_boxes
from mimi.credit.tools import CREDIT_TOOLS, CreditPorts, PhotoStore, credit_helper, credit_registry
from mimi.credit.verify import unknown_numbers
from mimi.harness.collector import BUILTIN_TOOLS
from mimi.harness.gateway import Gateway, ScriptedModel, say
from mimi.harness.loop import Harness
from mimi.harness.policy import GoPolicy
from mimi.harness.registry import MAX_TOOL_CHARS
from mimi.harness.types import RunEvent
from mimi.world.loader import load_world

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "eval" / "credit"
W = load_world()
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
SAM = CachedSegmenter(None, EVAL / "sam.cache.json", mode="replay")


def pic(cid: str) -> bytes:
    return (EVAL / "shelves" / f"{cid}.jpg").read_bytes()


class Boom:
    name = "boom"
    calls = 0

    def segment(self, image: bytes, mime: str, prompt: str) -> Segmentation:
        self.calls += 1
        raise AssertionError("SAM must not be called")


# ------------------------------------------------------------------ SAM client


LINE = ("<0f>0<|box;x1=10;y1=20;x2=60;y2=120;w=640;h=480;c=0.81|><|mask;x=0;y=0;c=0.81;"
        "data=10,5,!abc|>,2<|box;x1=70;y1=20;x2=120;y2=118;w=640;h=480|><|mask;x=0;y=0;"
        "data=10,5,~x~y|>")


def test_parse_boxes_reads_keys_with_and_without_confidence() -> None:
    boxes = parse_boxes(LINE)
    assert [(b.x1, b.y2, b.score) for b in boxes] == [(10, 120, 0.81), (70, 118, 1.0)]
    assert parse_boxes("") == ()


def test_meta_sam_request_shape_retry_and_no_key_in_errors() -> None:
    seen: list[dict[str, Any]] = []
    answers = [httpx.Response(503), httpx.Response(200, json={
        "output": [{"content": [{"type": "output_text", "text": LINE}]}],
        "usage": {"images_processed": 1}})]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append({"url": str(req.url), "body": json.loads(req.content),
                     "auth": req.headers["authorization"]})
        return answers.pop(0)

    sam = MetaSam(api_key="k-test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    seg = sam.segment(b"\xff\xd8jpeg", "image/jpeg", "product")
    assert len(seg.boxes) == 2 and len(seen) == 2
    body = seen[0]["body"]
    assert seen[0]["url"].endswith("/v1/responses") and body["model"] == "sam-3.1"
    parts = body["input"][0]["content"]
    assert parts[0] == {"type": "input_text", "text": "product"}
    assert parts[1]["image_url"].startswith("data:image/jpeg;base64,")
    assert body["metadata"] == {"include_confidence": "true", "mask_encoding": "one_bit"}

    bad = MetaSam(api_key="k-secret", transport=httpx.MockTransport(  # pragma: allowlist secret
        lambda r: httpx.Response(400)), sleep=lambda s: None)
    with pytest.raises(SamError) as e:
        bad.segment(b"x", "image/jpeg", "product")
    assert "k-secret" not in str(e.value)


def test_cached_segmenter_replays_and_keeps_no_pixels(tmp_path: Path) -> None:
    class One:
        name = "sam-3.1"

        def segment(self, image: bytes, mime: str, prompt: str) -> Segmentation:
            return Segmentation(prompt, (Box(1, 2, 30, 40, 0.9, 64, 48),))

    path = tmp_path / "c.json"
    rec = CachedSegmenter(One(), path)
    rec.segment(b"pixels-123", "image/jpeg", "product")
    assert b"pixels-123".decode() not in path.read_text()
    replay = CachedSegmenter(None, path, mode="replay")
    assert replay.segment(b"pixels-123", "image/jpeg", "product").boxes[0].x2 == 30
    with pytest.raises(SamCacheMiss):
        replay.segment(b"other", "image/jpeg", "product")


def test_sam_cost_is_n4_a_photo() -> None:
    assert sam_cost_kobo(1) == 400 and sam_cost_kobo(25) == 10_000


# ------------------------------------------------------------------ photo + count


def test_photo_checks_refuse_blurry_dark_small_and_junk() -> None:
    rows = ((Run("OIL-1L", 5),), (Run("SODA-50CL", 4),))
    ok, _ = render(ShelfSpec(seed=1, rows=rows))
    blurry, _ = render(ShelfSpec(seed=1, rows=rows, quality="blurry"))
    dark, _ = render(ShelfSpec(seed=1, rows=rows, quality="dark"))
    assert check_photo(ok).usable
    assert check_photo(blurry).issue == "blurry"
    assert check_photo(dark).issue == "too_dark"
    assert check_photo(b"not an image").issue == "unreadable"
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (300, 200), (120, 120, 120)).save(buf, format="JPEG")
    assert check_photo(buf.getvalue()).issue == "too_small"


def test_bad_photo_never_reaches_sam() -> None:
    boom = Boom()
    out = count_shelf(pic("s09"), ["NDL-70-CTN"], {}, boom)
    assert not out.usable and boom.calls == 0 and out.items == ()


def test_count_matches_truth_values_in_kobo_and_skips_other_stock() -> None:
    case = next(c for c in load_cases() if c["id"] == "s01")
    go = CreditGo(W)
    prices = go.price_list(W.tenant_id)
    hist = go.credit_history(W.tenant_id, case["shop_id"])
    f = credit_file(hist, go.shop(W.tenant_id, case["shop_id"]),
                    go.receivables(W.tenant_id, case["shop_id"]), prices)
    out = count_shelf(pic("s01"), f.candidates, prices, SAM)
    assert out.counts() == case["truth_counted"]
    assert out.other_pieces == case["other_pieces"] > 0
    for item in out.items:
        price, pieces, _, _ = prices[item.sku]
        assert item.value_kobo == item.about * price // pieces
    assert out.total_value_kobo == sum(i.value_kobo for i in out.items)
    # Only delivered items count: with no candidates nothing is counted.
    assert count_shelf(pic("s01"), [], prices, SAM).items == ()


def test_keep_boxes_drops_duplicates_and_group_boxes() -> None:
    a = Box(0, 0, 100, 100, 0.9, 1000, 1000)
    dup = Box(2, 2, 100, 100, 0.8, 1000, 1000)
    b = Box(120, 0, 220, 100, 0.9, 1000, 1000)
    group = Box(0, 0, 230, 105, 0.7, 1000, 1000)
    low = Box(300, 0, 400, 100, 0.1, 1000, 1000)
    assert keep_boxes([a, dup, b, group, low], (1000, 1000)) == [a, b]


def test_looks_and_pieces() -> None:
    sku = {s.sku: s for s in W.skus}
    assert pieces_per_unit(sku["NDL-70-CTN"]) == 40 and pieces_per_unit(sku["OIL-1L"]) == 1
    assert item_name(sku["NDL-70-CTN"]) == "Sunrise Instant Noodles 70g"
    assert set(LOOKS) <= set(sku)


# ------------------------------------------------------------------ recommendation


def _file(**kw: Any) -> CreditFile:
    base: dict[str, Any] = dict(
        shop_id="shop-x", shop_name="X", current_limit_kobo=100_000_00,
        outstanding_kobo=0, overdue_kobo=0, oldest_days_overdue=0, window_days=56,
        deliveries=8, delivered_kobo=400_000_00, verified_payments=10, verified_paid_kobo=1,
        verified_on_time=10, on_time_pct=100, unverified_payments=0, unverified_kobo=0,
        promises_made=0, promises_broken=0, open_disputes=0, utilisation_pct=0,
        recent_pieces={"NDL-70-CTN": 40}, candidates=("NDL-70-CTN",))
    base.update(kw)
    return CreditFile(**base)


def test_recommendation_rules() -> None:
    assert recommend(_file(), None).outcome == "support"
    assert recommend(_file(verified_payments=2, verified_on_time=2), None).outcome == "hold"
    assert recommend(_file(verified_payments=0, verified_on_time=0, on_time_pct=0),
                     None).outcome == "hold"
    late = _file(overdue_kobo=5_000_00, oldest_days_overdue=30)
    assert recommend(late, None).outcome == "not_now"
    assert recommend(_file(promises_made=3, promises_broken=2), None).outcome == "not_now"
    assert recommend(_file(verified_on_time=4, on_time_pct=40), None).outcome == "not_now"


def test_shelf_is_supporting_only_and_flags_borrowed_stock() -> None:
    from mimi.credit.photo import PhotoCheck
    from mimi.credit.shelf import ItemCount, ShelfCount

    def shelf(n: int) -> ShelfCount:
        item = ItemCount("NDL-70-CTN", "Sunrise Instant Noodles 70g", "packet", "packets", n,
                         "high", 1_275_000, 40, n * 1_275_000 // 40, 0.8, 0.6)
        return ShelfCount("ph_x", PhotoCheck(True, "", 1280, 960, 120, 300), (item,), 0, n,
                          item.value_kobo, "high", 2)

    assert recommend(_file(), shelf(20)).outcome == "support"
    borrowed = recommend(_file(), shelf(200))
    assert borrowed.outcome == "hold" and borrowed.flags == ("shelf_more_than_delivered:NDL-70-CTN",)
    # A full shelf never lifts a late payer.
    late = _file(overdue_kobo=5_000_00, oldest_days_overdue=30)
    assert recommend(late, shelf(20)).outcome == "not_now"
    assert about_kobo(7_790_625) == 7_790_000


# ------------------------------------------------------------------ verifier + copy


def test_verifier_flags_numbers_not_in_the_facts() -> None:
    facts = [{"verified_paid_kobo": 741_430_000, "verified_on_time": 17},
             "17 of 17 payments", "Sunrise Instant Noodles 70g"]
    assert unknown_numbers("17 of 17 on time, N7,414,300 verified, 70g packs", facts) == []
    assert unknown_numbers("raise to N2,000,000 within 30 days", facts) == ["N2,000,000", "30"]


def test_shop_copy_names_the_distributor_and_says_they_decide() -> None:
    assert "They decide the limit." in copy.no_count("Coral Lane Trading")
    assert "one clear photo" in copy.ask_photo("Coral Lane Trading")


# ------------------------------------------------------------------ tools + policy


def _ports() -> CreditPorts:
    return CreditPorts(PhotoStore(), SAM)


def test_toolset_builds_under_size_with_never_tools_refused() -> None:
    reg = credit_registry(_ports())
    helper = credit_helper(reg, GoPolicy())
    assert set(helper.toolset.names) == set(CREDIT_TOOLS + BUILTIN_TOOLS)
    for name in CREDIT_TOOLS:
        assert reg.get(name).size() < MAX_TOOL_CHARS
    assert all(never_suite(W)["checks"].values())


def test_submit_refuses_invented_figures_and_a_second_request() -> None:
    reg = credit_registry(_ports())
    policy = GoPolicy()
    helper = credit_helper(reg, policy)
    go = CreditGo(W)

    def run(note: str, sid: str = "shop-003") -> Any:
        def script(messages: Any, tools: Any) -> Any:
            tool_msgs = [m for m in messages if m.get("role") == "tool"]
            if not tool_msgs:
                return say("", ("prepare_credit_review", {"shop_id": sid}))
            try:
                data = json.loads(tool_msgs[-1]["content"]).get("data") or {}
            except ValueError:
                return say("")
            if "review_id" in data and len(tool_msgs) == 1:
                return say("", ("submit_credit_review", {"shop_id": sid,
                                                         "review_id": data["review_id"],
                                                         "note_for_owner": note}))
            return say("")
        h = Harness(book=go, control=go, events=go, gateway=Gateway([ScriptedModel(script)]),
                    policy=policy, clock=lambda: NOW)
        return h.run(RunEvent(event_id=f"e-{note[:5]}", tenant_id=W.tenant_id, thread_id="t",
                              wake_source="shop_message", sender="shop", shop_id=sid,
                              text="more credit", at=NOW), helper)

    bad = run("Give them N5,000,000 now.")
    assert not bad.intents
    assert any("not in the reasons" in e.short for e in bad.events if e.type == "tool_result")
    good = run("Pays on time; you decide.")
    assert [i.tier for i in good.intents] == ["approve"] and good.status == "parked_for_approval"
    for i in good.intents:
        go.submit_record(i)
    assert go.open_credit_review(W.tenant_id, "shop-003")
    again = run("Second ask; you decide.")
    assert not again.intents


# ------------------------------------------------------------------ scene


def test_demo_scene_matches_the_issue_and_never_sets_a_limit() -> None:
    sc = CreditScene(W, scripted_model(), SAM, offline_router())
    sc.shop_text("shop-003", "Abeg, increase my credit, market dey move.")
    first = sc.turns[-1]
    assert first.route["skill"] == "credit/credit-request"
    assert first.shop_messages == [copy.ask_photo("Coral Lane Trading")]
    sc.shop_photo("shop-003", pic("s01"))
    second = sc.turns[-1]
    assert second.route["rule"] == "pending_answer:photo"
    reply = second.shop_messages[-1]
    assert reply.startswith("I counted about ") and "They decide the limit." in reply
    (item,) = sc.approvals()
    p = item["payload"]
    assert p["status"] == "draft" and p["changes_limit"] is False and p["moves_money"] is False
    assert p["buttons"] == ["approve", "decline"] and p["shelf"]["items"]
    assert sc.go.accepted[item["idempotency_key"]].outcome == "queued_for_approval"
    assert all(s.credit_limit_kobo == t.credit_limit_kobo
               for s, t in zip(sc.world.shops, load_world().shops, strict=True))


def test_photo_bytes_never_enter_the_model_context() -> None:
    sc = CreditScene(W, scripted_model(), SAM, offline_router())
    sc.shop_text("shop-003", "Abeg, increase my credit")
    sc.shop_photo("shop-003", pic("s01"))
    for m in sc.harness.gateway.models[0].seen[-1]:  # type: ignore[attr-defined]
        text = json.dumps(m)
        assert "base64" not in text and "data:image" not in text


@pytest.mark.parametrize("cv", CONVOS, ids=[c.cid for c in CONVOS])
def test_scripted_conversations(cv: Any) -> None:
    sc, turns = run_convo(W, cv, scripted_model(), SAM, offline_router())
    res = judge_convo(cv, sc, turns)
    assert res["ok"], res["why"]


def test_recorded_live_conversations_replay() -> None:
    from mimi.credit.eval import MODEL, SCENE_CACHE, router_model
    from mimi.harness import fastclass
    from mimi.harness.gateway import RecordedToolModel
    from mimi.harness.router import Router

    rec = RecordedToolModel(SCENE_CACHE, None, name=MODEL)
    router = Router(model=router_model(False), fast=fastclass.model())
    for cv in CONVOS:
        sc, turns = run_convo(W, cv, rec, SAM, router)
        assert judge_convo(cv, sc, turns)["ok"], cv.cid


def test_credit_module_rules() -> None:
    """The harness never imports Credit; Credit never imports another flow."""
    def imports(path: Path) -> set[str]:
        out: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                out.add(node.module)
            elif isinstance(node, ast.Import):
                out.update(a.name for a in node.names)
        return out

    for path in (ROOT / "mimi" / "harness").rglob("*.py"):
        assert not any(n.startswith("mimi.credit") for n in imports(path)), path
    for path in (ROOT / "mimi" / "credit").rglob("*.py"):
        for n in imports(path):
            assert not n.startswith(("mimi.sim", "mimi.receipts", "mimi.proof", "mimi.voice",
                                     "mimi.stock", "mimi.api", "mimi.store")), (path, n)


def test_fixture_hashes_match_labels() -> None:
    import hashlib

    for c in load_cases():
        assert hashlib.sha256(pic(c["id"])).hexdigest() == c["sha256"], c["id"]
