"""Credit exposure (feat/returns-and-exposure): the bands and weights, the
confidence rule, verified versus reported payments, and the API shape. Code
only: no model call anywhere."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.credit import exposure as ex
from mimi.harness.gateway import ScriptedModel
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.world.loader import load_world
from tests.test_api import script

W = load_world()
A = "coral-lane"
B = "second-lane"
OWNER = "s-owner-key-000000000001"
OTHER = "s-other-key-00000000001"
ID_RE = re.compile(r"\b(?:shop|dlv|apr|ret|crn)-?\d|\bshop_\w|_kobo")


@pytest.fixture()
def api(store_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("MIMI_DEMO_WIRING", "1")
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W, stock_demo=True)
    seed_world(engine, W, tenant_id=B)
    agent = Agent(engine, models=[ScriptedModel(script)], worker_id="test-worker")
    app = create_app(Settings(keys=parse_keys(f"{A}:owner:{OWNER},{B}:owner:{OTHER}"),
                              wait_s=5), agent, workers=0)
    with TestClient(app) as client:
        yield {"c": client, "engine": engine}
    engine.dispose()


def h() -> dict[str, str]:
    return {"Authorization": f"Bearer {OWNER}"}


def h2() -> dict[str, str]:
    return {"Authorization": f"Bearer {OTHER}"}


# ------------------------------------------------------------- the bands


def test_bucket_boundaries_are_the_contract() -> None:
    assert ex.bucket_of(0) == "current"
    assert ex.bucket_of(-5) == "current"
    assert ex.bucket_of(1) == "days_1_30"
    assert ex.bucket_of(30) == "days_1_30"
    assert ex.bucket_of(31) == "days_31_60"
    assert ex.bucket_of(60) == "days_31_60"
    assert ex.bucket_of(61) == "days_over_60"
    assert ex.bucket_of(999) == "days_over_60"
    assert [b.key for b in ex.BUCKETS] == ["current", "days_1_30", "days_31_60",
                                            "days_over_60"]
    assert [b.weight_pct for b in ex.BUCKETS] == [100, 125, 150, 200]


def test_weighted_kobo_scales_the_money_by_age() -> None:
    assert ex.weighted_kobo(100_000, 0) == 100_000
    assert ex.weighted_kobo(100_000, 10) == 125_000
    assert ex.weighted_kobo(100_000, 45) == 150_000
    assert ex.weighted_kobo(100_000, 90) == 200_000


def test_confidence_is_defined_by_verified_history() -> None:
    assert ex.confidence_for(0) == "low"
    assert ex.confidence_for(3) == "low"
    assert ex.confidence_for(4) == "medium"
    assert ex.confidence_for(7) == "medium"
    assert ex.confidence_for(8) == "high"
    assert ex.confidence_for(50) == "high"


# ------------------------------------------------------------- the figures


def _pay(day: int, amount: int, level: str, *, fake_alert: bool = False) -> dict[str, Any]:
    return {"shop_id": "shop-a", "payment_id": f"p{day}", "day": day,
            "amount_kobo": 0 if fake_alert else amount, "claimed_kobo": amount,
            "proof_level": level, "proof_policy_version": "v1", "witnesses": "[]"}


def test_shop_exposure_counts_only_bank_verified_payments() -> None:
    recs = [{"delivery_id": "dlv-a", "outstanding_kobo": 100_000, "days_overdue": 0},
            {"delivery_id": "dlv-b", "outstanding_kobo": 200_000, "days_overdue": 70}]
    pays = [_pay(50, 30_000, "verified"), _pay(51, 40_000, "likely"),
            _pay(52, 9_000, "estimated")]
    row = ex.shop_exposure("shop-a", "Shop A", recs, pays, as_of_day=56)
    assert row.outstanding_kobo == 300_000
    assert row.overdue_kobo == 200_000 and row.oldest_days_overdue == 70
    assert row.bucket_kobo == {"current": 100_000, "days_over_60": 200_000}
    assert row.exposure_kobo == 100_000 + 2 * 200_000
    assert row.verified_paid_kobo == 30_000  # the reported ones never count
    assert row.shown_not_counted_kobo == 49_000
    assert row.confidence == "low"
    assert not ID_RE.search(row.as_dict()["note"])


def test_total_confidence_is_the_weakest_shop() -> None:
    strong = ex.shop_exposure("shop-a", "A", [
        {"delivery_id": "d1", "outstanding_kobo": 1_000, "days_overdue": 0}],
        [_pay(i, 1_000, "verified") for i in range(9)], as_of_day=56)
    weak = ex.shop_exposure("shop-b", "B", [
        {"delivery_id": "d2", "outstanding_kobo": 1_000, "days_overdue": 5}],
        [], as_of_day=56)
    assert strong.confidence == "high" and weak.confidence == "low"
    total = ex.total_exposure([strong, weak])
    assert total.confidence == "low"
    assert total.exposure_kobo == strong.exposure_kobo + weak.exposure_kobo


def test_a_payment_outside_the_window_does_not_count() -> None:
    recs = [{"delivery_id": "d1", "outstanding_kobo": 5_000, "days_overdue": 0}]
    old = ex.shop_exposure("shop-a", "A", recs, [_pay(5, 9_000, "verified")],
                           as_of_day=56, window_days=30)
    assert old.verified_paid_kobo == 0 and old.shown_not_counted_kobo == 0
    inside = ex.shop_exposure("shop-a", "A", recs, [_pay(5, 9_000, "verified")],
                              as_of_day=56, window_days=56)
    assert inside.verified_paid_kobo == 9_000
    reported = ex.shop_exposure("shop-a", "A", recs, [_pay(50, 7_000, "likely")],
                                as_of_day=56)
    assert reported.verified_paid_kobo == 0 and reported.shown_not_counted_kobo == 7_000


# ------------------------------------------------------------- the endpoint


def test_exposure_endpoint_shape_and_no_ids(api: dict[str, Any]) -> None:
    r = api["c"].get("/v1/credit/exposure", headers=h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policy_version"] == "exposure-v0.1"
    assert body["as_of"]
    assert [b["key"] for b in body["buckets"]] == ["current", "days_1_30", "days_31_60",
                                                    "days_over_60"]
    assert "assumption" in body["note"]
    total = body["total"]
    assert total["shop_id"] == "" and total["shop_name"] == "All shops"
    assert total["outstanding_kobo"] > 0 and total["overdue_kobo"] > 0
    assert total["exposure_kobo"] >= total["outstanding_kobo"]
    per_shop = sum(s["outstanding_kobo"] for s in body["shops"])
    assert per_shop == total["outstanding_kobo"]
    per_bucket = {k: sum(s["buckets"][f"{k}_kobo"] for s in body["shops"])
                  for k in ("current", "days_1_30", "days_31_60", "days_over_60")}
    assert sum(per_bucket.values()) == total["outstanding_kobo"]
    assert total["confidence"] in ("low", "medium", "high")
    for row in body["shops"] + [total]:
        assert row["confidence"] in ("low", "medium", "high")
        assert row["outstanding_naira"].startswith("\u20a6")
        assert not ID_RE.search(row["note"])
        assert "\u2014" not in row["note"] and "\u2013" not in row["note"]


def test_exposure_is_tenant_filtered(api: dict[str, Any]) -> None:
    c, engine = api["c"], api["engine"]
    mine = c.get("/v1/credit/exposure", headers=h()).json()
    theirs = c.get("/v1/credit/exposure", headers=h2()).json()
    assert mine["total"]["outstanding_kobo"] > 0
    assert mine["total"]["outstanding_kobo"] == theirs["total"]["outstanding_kobo"]
    # Empty the second distributor's receivables: its figures must fall to
    # zero while the first's are untouched.
    with engine.begin() as conn:
        conn.execute(sa.delete(s.receivables).where(s.receivables.c.tenant_id == B))
    mine2 = c.get("/v1/credit/exposure", headers=h()).json()
    theirs2 = c.get("/v1/credit/exposure", headers=h2()).json()
    assert theirs2["total"]["outstanding_kobo"] == 0
    assert all(x["outstanding_kobo"] == 0 for x in theirs2["shops"])
    assert mine2["total"]["outstanding_kobo"] == mine["total"]["outstanding_kobo"]


def test_exposure_weights_are_visible_with_the_figures(api: dict[str, Any]) -> None:
    body = api["c"].get("/v1/credit/exposure", headers=h()).json()
    weights = {b["key"]: b["weight_pct"] for b in body["buckets"]}
    assert weights == {"current": 100, "days_1_30": 125, "days_31_60": 150,
                       "days_over_60": 200}
