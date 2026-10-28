"""Photo routing eval (RAI-304): a shop's camera photo reaches the right helper.

    python -m mimi.api.photo_route_eval                  # fresh set, replay (CI)
    python -m mimi.api.photo_route_eval --live           # record what is missing (META_API_KEY)
    python -m mimi.api.photo_route_eval --dev [--live]   # the dev set, for tuning only
    python -m mimi.api.photo_route_eval --words-only     # the router before RAI-304 (no look)

Cases: ``eval/photo_route/fresh.jsonl`` (27, written and committed before
the routing change, scored once) and ``dev.jsonl`` (9, other photos and
wording, for tuning). Half are handwritten order lists with vague, Pidgin,
Yoruba, Hausa, Igbo, emoji or empty captions; the rest are look-alikes
that must never reach Order: shelf photos, a POS slip, a printed invoice, a
bank teller, a registration certificate, a handwritten list of payments, a
payment screenshot, and list photos captioned as a complaint or a payment.

Each case goes through the real router (rules, phrases, the fast
classifier, Muse Spark) with the agent's photo look: the Order list reader
on Muse Spark vision over the photo bytes. Every model answer is recorded
once and replayed in CI; a replay miss fails the run.

Scored in code: a list case is right when it is routed to
``order/order-intake``; a look-alike is right when it is not routed to
Order and lands on one of its allowed skills, or asks ("clarify"). Pass
mark 90% of cases right; held at zero: a look-alike routed to Order.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from mimi.harness.versions import ROUTER_VERSIONS
from mimi.tz import LAGOS

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "eval"
DIR = EVAL / "photo_route"
NOW = datetime(2026, 10, 28, 10, 15, tzinfo=LAGOS)
TENANT = "coral-lane"
MODEL = "muse-spark-1.3"
PASS = 0.90
ORDER = "order/order-intake"


def load(split: str) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (DIR / f"{split}.jsonl").read_text(encoding="utf-8")
            .splitlines() if x.strip()]


def score(case: dict[str, Any], status: str, skill: str) -> tuple[bool, bool]:
    """(right, look-alike routed to Order)."""
    to_order = status == "routed" and skill == ORDER
    if case["want"] == "order":
        return to_order, False
    ok = set(case.get("ok") or ())
    right = not to_order and ((status == "clarify" and "clarify" in ok)
                              or (status == "routed" and skill in ok))
    return right, to_order


def run(split: str, live: bool, words_only: bool) -> dict[str, Any]:
    from PIL import Image

    from mimi.harness import fastclass
    from mimi.harness.binding import AnswerBinder
    from mimi.harness.gateway import kobo_cost
    from mimi.harness.inbound import Inbound, Media, Payload
    from mimi.harness.router import Router, ThreadState
    from mimi.models.cache import CachedModel
    from mimi.models.env import from_env
    from mimi.orders.reading import ListReader
    from mimi.store import ensure_schema, make_engine, seed_world
    from mimi.store.book import SqlGo
    from mimi.world.loader import load_world

    if live and not os.environ.get("META_API_KEY", "").strip():
        raise SystemExit("--live needs META_API_KEY")
    inner = from_env("mimi") if live else None
    mode: Literal["through", "replay"] = "through" if live else "replay"
    router_m = CachedModel(inner, DIR / f"{split}.router.cache.json", mode=mode, name=MODEL,
                           versions=ROUTER_VERSIONS)
    vision_m = CachedModel(inner, DIR / f"{split}.vision.cache.json", mode=mode, name=MODEL)
    reader = ListReader(vision_m)
    world = load_world()
    engine = make_engine("sqlite://")
    ensure_schema(engine)
    seed_world(engine, world)
    directory = SqlGo(engine).directory(TENANT)
    files: dict[str, Path] = {}
    errors: list[str] = []

    def see(ib: Inbound) -> str:
        path = files.get(ib.external_id)
        if path is None:
            return ""
        try:
            r = reader(path.read_bytes(), ib.payload.media.mime)
        except Exception as exc:  # the router falls back to words; the run fails
            errors.append(f"{ib.external_id}: vision {type(exc).__name__}"[:200])
            raise
        if r.via != "model":
            return ""
        return "order_list" if r.is_order_list and r.lines else "not_list"

    router = Router(model=router_m, fast=fastclass.model(), price=kobo_cost,
                    binder=AnswerBinder(model=router_m), see=None if words_only else see)
    rows: list[dict[str, Any]] = []
    for c in load(split):
        path = EVAL / c["photo"]
        data = path.read_bytes()
        with Image.open(path) as im:
            w, h = im.size
        ext = f"photo_route.{split}.{c['id']}"
        files[ext] = path
        shop = world.shop(c["shop_id"])
        ib = Inbound(source="shop_message", org_id=TENANT, external_id=ext, received_at=NOW,
                     payload=Payload(from_phone=shop.phone, media=Media(
                         kind="image", mime="image/jpeg",
                         sha256=hashlib.sha256(data).hexdigest(), width=w, height=h,
                         camera_exif=bool(c.get("camera", True)), caption=c["caption"])))
        try:
            d = router.route(ib, directory, ThreadState())
        except Exception as exc:  # a replay miss: the run fails, never a silent fake
            errors.append(f"{c['id']}: {type(exc).__name__}: {exc}"[:200])
            rows.append({"id": c["id"], "want": c["want"], "right": False, "error": str(exc)})
            continue
        ok, bad = score(c, d.status, d.skill)
        rows.append({"id": c["id"], "want": c["want"], "caption": c["caption"],
                     "photo": c["photo"], "status": d.status, "skill": d.skill,
                     "rule": d.rule, "layer": d.layer, "right": ok,
                     "lookalike_to_order": bad})
    n = len(rows)
    right = sum(r["right"] for r in rows)
    lists = [r for r in rows if r["want"] == "order"]
    looks = [r for r in rows if r["want"] != "order"]
    live_kobo = (kobo_cost(MODEL, router_m.live_prompt_tokens, router_m.live_completion_tokens)
                 + kobo_cost(MODEL, vision_m.live_prompt_tokens,
                             vision_m.live_completion_tokens))
    rate = round(right / max(1, n), 4)
    to_order = sum(bool(r.get("lookalike_to_order")) for r in rows)
    rep: dict[str, Any] = {
        "split": split, "mode": "live" if live else "replay",
        "router": "words_only" if words_only else "with_photo_look",
        "cases": n, "correct": right, "correct_rate": rate,
        "lists_to_order": f"{sum(r['right'] for r in lists)}/{len(lists)}",
        "lookalikes_right": f"{sum(r['right'] for r in looks)}/{len(looks)}",
        "lookalikes_to_order": to_order,
        "model_calls": {"router": router_m.calls, "vision": vision_m.calls},
        "live_calls": {"router": router_m.live_calls, "vision": vision_m.live_calls},
        "live_cost_kobo_this_run": live_kobo, "errors": errors, "rows": rows,
    }
    rep["passed"] = not errors and rate >= PASS and to_order == 0
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--dev", action="store_true")
    ap.add_argument("--words-only", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)
    split = "dev" if a.dev else "fresh"
    rep = run(split, a.live, a.words_only)
    for r in rep["rows"]:
        mark = "ok  " if r["right"] else "MISS"
        print(f"{r['id']}  {mark} want={r['want']:<9} got={r.get('status', '?')}:"
              f"{r.get('skill', '?')} [{r.get('rule', '')}] {r.get('caption', '')!r}")
    print(f"{split} ({rep['router']}): {rep['correct']}/{rep['cases']} right "
          f"({rep['correct_rate']:.1%}); lists to Order {rep['lists_to_order']}, "
          f"look-alikes right {rep['lookalikes_right']}, look-alikes to Order "
          f"{rep['lookalikes_to_order']}; live cost N{rep['live_cost_kobo_this_run'] / 100:.2f}"
          f"; {'PASS' if rep['passed'] else 'FAIL'}")
    for e in rep["errors"]:
        print("  error", e)
    if not a.no_write:
        name = f"{split}{'.words_only' if a.words_only else ''}.report.json"
        (DIR / name).write_text(json.dumps(rep, indent=1, sort_keys=True,
                                           ensure_ascii=False) + "\n")
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
