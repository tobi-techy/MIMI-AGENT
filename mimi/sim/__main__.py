"""``python -m mimi.sim`` runs the live day.

    --shops N          first N shops of the live day (default all 40)
    --hours H          how long the day runs on the sim clock (default 26)
    --realtime SPEED   sleep between events: 1 = real time, 60 = 1h per minute
    --db URL           persist jobs, shop replies and voice-reply consents
                       (Postgres or SQLite URL)
    --transcript       print the WhatsApp transcript
    --cache PATH       record live model answers to PATH and replay them on a
                       rerun (a long live run can be resumed; tokens reported
                       include the replayed calls' recorded usage)
    --voice SHARE      shops send synthetic voice notes: SHARE (0 to 1) of
                       shops answer the receipt with a voice clip, and every
                       "[voice note]" placeholder becomes one (default 0: off)
    --promises SHARE   of the shops whose first reply is a voice note, SHARE
                       send one that also promises to pay ("I go pay 20k
                       tomorrow"); Mimi reads it back and waits for YES
    --screenshots SHARE  of the shops that pay by transfer, SHARE send a
                       payment screenshot for the day's delivery (drawn on the
                       fly, fictional banks); Mimi reads it with Muse Spark,
                       checks it in code and marks it likely paid, and the
                       sim's bank feed later witnesses the genuine ones
    --fakes SHARE      of those, SHARE send a fake screenshot instead
    --shop-ids A,B     run exactly these shops (instead of --shops N)
    --voice-replies    every shop sends "VOICE ON" at the start (its consent),
                       and Mimi's short replies also go as a voice note
                       (Spitch speech with SPITCH_API_KEY, else the fake)

Models come from the environment (``META_API_KEY``, ``MIMI_MODEL``,
``SIM_MODEL``); with no key both sides use the deterministic FakeModel. The
transcriber comes from ``mimi.voice.env`` (``SPITCH_API_KEY``,
``META_API_KEY``, ``MIMI_VOICE``); with no key it is the fake.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from mimi.models import from_env
from mimi.models.cache import CachedModel
from mimi.receipts.verdict import fake_verdict
from mimi.sim.engine import Simulator
from mimi.sim.persona import fake_shop
from mimi.voice.env import speaker_from_env, transcriber_from_env
from mimi.world import load_world


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.sim")
    ap.add_argument("--shops", type=int, default=None)
    ap.add_argument("--hours", type=float, default=26.0)
    ap.add_argument("--realtime", type=float, default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--transcript", action="store_true")
    ap.add_argument("--cache", default=None)
    ap.add_argument("--voice", type=float, default=0.0)
    ap.add_argument("--promises", type=float, default=0.0)
    ap.add_argument("--voice-replies", action="store_true")
    ap.add_argument("--screenshots", type=float, default=0.0)
    ap.add_argument("--fakes", type=float, default=0.0)
    ap.add_argument("--shop-ids", default="")
    args = ap.parse_args(argv)

    world = load_world()
    shop_ids = None
    if args.shop_ids:
        shop_ids = [s.strip() for s in args.shop_ids.split(",") if s.strip()]
    elif args.shops:
        shop_ids = [d.shop_id for d in world.live_deliveries][: args.shops]
    mimi: Any = from_env("mimi", fake=fake_verdict)
    sim: Any = from_env("sim", fake=fake_shop)
    if args.cache and not mimi.name.startswith("fake"):
        mimi = CachedModel(mimi, args.cache + ".mimi.json")
        # The screenshot reader shares Mimi's cache file (keys include the image hash).
        sim = CachedModel(sim, args.cache + ".sim.json")
    extra: dict[str, Any] = {}
    if args.db:
        import sqlalchemy as sa

        from mimi.receipts.inbox import SqlShopInbox
        from mimi.runtime.sql_store import SqlJobStore
        from mimi.voice.prefs import SqlVoicePrefs

        engine = sa.create_engine(args.db)
        store, inbox = SqlJobStore(engine), SqlShopInbox(engine)
        prefs = SqlVoicePrefs(engine)
        from mimi.proof.ledger import SqlProofLedger

        ledger = SqlProofLedger(engine)
        if args.db.startswith("sqlite"):
            store.ensure_schema()
            inbox.ensure_schema()
            prefs.ensure_schema()
            ledger.ensure_schema()
        extra = {"store": store, "inbox": inbox, "voice_prefs": prefs, "proof_ledger": ledger}
    transcriber = None
    if args.voice > 0:
        transcriber = transcriber_from_env(cache=(args.cache + ".voice") if args.cache else None)
    speaker = None
    if args.voice_replies:
        speaker = speaker_from_env(cache=(args.cache + ".tts") if args.cache else None)
    started = time.monotonic()
    runner = Simulator(
        world,
        mimi_model=mimi,
        sim_model=sim,
        shop_ids=shop_ids,
        realtime_speed=args.realtime,
        seed=args.seed,
        hours=args.hours,
        transcriber=transcriber,
        voice_share=args.voice,
        promise_share=args.promises,
        voice_replies=args.voice_replies,
        speaker=speaker,
        screenshot_share=args.screenshots,
        fake_share=args.fakes,
        **extra,
    )
    report = runner.run()
    report["mimi_model"] = mimi.name
    report["sim_model"] = sim.name
    if transcriber is not None:
        report["transcriber"] = transcriber.name
    if speaker is not None:
        report["speaker"] = speaker.name
    if isinstance(mimi, CachedModel):
        report["live_calls_this_pass"] = mimi.live_calls + sim.live_calls
        report["replayed_calls"] = mimi.cached_calls + sim.cached_calls
    report["wall_seconds"] = round(time.monotonic() - started, 2)
    if args.transcript:
        print("\n".join(runner.transcript.lines))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
