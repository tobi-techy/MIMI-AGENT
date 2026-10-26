"""Synthetic voice notes in the one-day simulator."""

from __future__ import annotations

from mimi.models import FakeModel
from mimi.receipts.verdict import fake_verdict
from mimi.sim.engine import Simulator, voice_clip_for
from mimi.sim.persona import fake_shop
from mimi.voice import FakeTranscriber
from mimi.world import load_world

WORLD = load_world()


def test_sim_with_voice_runs_offline() -> None:
    shop_ids = [d.shop_id for d in WORLD.live_deliveries][:3]
    sim = Simulator(
        WORLD,
        mimi_model=FakeModel(fake_verdict),
        sim_model=FakeModel(fake_shop),
        shop_ids=shop_ids,
        transcriber=FakeTranscriber(),
        voice_share=1.0,
    )
    report = sim.run()
    assert report["voice_notes"] >= 3
    assert report["confirmed_despite_shortage"] == 0
    assert report["shortages_flagged"] == report["shortages_in_world"]
    assert report["still_waiting"] == 0


def test_voice_clip_matches_truth_and_language() -> None:
    short = next(d for d in WORLD.live_deliveries if d.shortage)
    clip = voice_clip_for(short.shop_id, True)
    assert clip is not None and clip["category"] == "dispute"
    full = next(d for d in WORLD.live_deliveries if not d.shortage)
    clip = voice_clip_for(full.shop_id, False)
    assert clip is not None and clip["category"] == "confirm"
