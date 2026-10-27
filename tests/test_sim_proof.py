"""The simulator with shops paying by screenshot, offline (RAI-268)."""

from __future__ import annotations

from mimi.models import FakeModel
from mimi.receipts.verdict import fake_verdict
from mimi.sim.engine import Simulator
from mimi.sim.persona import fake_shop
from mimi.world import load_world

SHOPS = ["shop-017", "shop-006", "shop-034", "shop-019", "shop-037", "shop-025"]


def run(fakes: float) -> tuple[Simulator, dict]:
    world = load_world()
    sim = Simulator(
        world,
        mimi_model=FakeModel(fake_verdict, name="fake:mimi"),
        sim_model=FakeModel(fake_shop, name="fake:sim"),
        shop_ids=SHOPS,
        screenshot_share=1.0,
        fake_share=fakes,
    )
    return sim, sim.run()


def test_screenshots_are_likely_then_verified_only_by_the_bank() -> None:
    sim, report = run(0.0)
    assert report["screenshots_sent"] >= len(SHOPS)
    assert report["screenshot_fakes_sent"] == 0
    assert report["proofs_likely_recorded"] == len(SHOPS)
    # Every genuine payment reaches the sim's bank feed, so each is witnessed.
    assert report["proofs_verified_by_bank"] == len(SHOPS)
    assert report["verified_without_bank_credit"] == 0
    for proof in sim.go.proofs.values():
        assert proof["level"] == "likely"
    for w in sim.go.witnessed.values():
        assert w["credit_id"].startswith("sim-credit-")
    # A resend is answered as a duplicate and never matched twice.
    assert report["proof_verdicts"].get("duplicate", 0) == report["screenshot_resends"]


def test_fake_screenshots_go_to_approvals_never_likely() -> None:
    sim, report = run(1.0)
    assert report["screenshot_fakes_sent"] == len(SHOPS)
    assert report["fakes_marked_likely"] == 0
    assert report["proofs_likely_recorded"] == 0
    assert report["proofs_verified_by_bank"] == 0
    assert report["proofs_to_approvals"] == len(SHOPS)
    for m in sim.go.to_shops:
        if m["purpose"] == "proof_review":
            assert "confirm with the bank" in m["text"]
            assert "fake" not in m["text"].lower()
