#!/usr/bin/env python3
"""Make the 30 synthetic shelf photos (24 gated, 6 stress) and their labels (RAI-271).

    python scripts/make_shelf_fixtures.py             # writes eval/credit/
    python scripts/make_shelf_fixtures.py --only x01  # draw one photo, keep the rest

Writes ``eval/credit/shelves/<id>.jpg`` and ``eval/credit/shelves.jsonl``:
one case per photo with the shop (from the Coral Lane world), the true
number of pieces of each item drawn, which of those the shop's distributor
delivered (the only ones Mimi may count), the photo quality, the split and
the recommendation label.

Everything is synthetic and labelled so: invented brands, invented shops,
packs drawn with Pillow (``mimi.credit.render``). 20 photos can be counted
and 4 cannot (2 blurry, 2 too dark). Split: dev 10 (8 countable, 2 not),
held-out 14 (12 countable, 2 not). Thresholds are tuned on dev only.

Labels are written from the world's ground truth, which Credit never sees
(each shop's ``pays`` trait), plus one twist per case where it applies:

* ``support``: a shop that pays on time, by bank transfer;
* ``hold``: a shop that pays on time but in cash to the rep (nothing the bank
  feed can verify yet), or a good payer whose shelf shows far more of an item
  than Coral Lane delivered in four weeks (borrowed or bought elsewhere);
* ``not_now``: a shop that pays late or disputes and is weeks overdue.

Six more ``stress`` photos (small, crowded and overlapping, tilted, dim,
soft) are reported in the eval but not gated: they show where counting from
one phone photo starts to break.

The layouts are fixed by the seed; the sensor noise comes from Pillow's own
generator and JPEG bytes can differ across Pillow versions, so CI does not
regenerate: it checks the committed files against
the SHA-256 in the labels.
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mimi.credit.looks import LOOKS, pieces_per_unit  # noqa: E402
from mimi.credit.render import OTHER_LOOKS, Run, ShelfSpec, render  # noqa: E402
from mimi.credit.review import SHELF_SLACK, SHELF_SLACK_PIECES  # noqa: E402
from mimi.world import load_world  # noqa: E402

OUT = ROOT / "eval" / "credit"
SEED = 2710

# (case id, shop, split, label, twist)
CASES: tuple[tuple[str, str, str, str, str], ...] = (
    ("s01", "shop-003", "dev", "support", ""),
    ("s02", "shop-016", "dev", "support", ""),
    ("s03", "shop-029", "dev", "support", ""),
    ("s04", "shop-013", "dev", "hold", "borrowed"),
    ("s05", "shop-015", "dev", "hold", "cash"),
    ("s06", "shop-002", "dev", "not_now", ""),
    ("s07", "shop-010", "dev", "not_now", ""),
    ("s08", "shop-001", "dev", "not_now", ""),
    ("s09", "shop-005", "dev", "not_now", "blurry"),
    ("s10", "shop-020", "dev", "hold", "dark"),
    ("s11", "shop-011", "test", "support", ""),
    ("s12", "shop-019", "test", "support", ""),
    ("s13", "shop-024", "test", "support", ""),
    ("s14", "shop-031", "test", "support", ""),
    ("s15", "shop-038", "test", "support", ""),
    ("s16", "shop-035", "test", "hold", "borrowed"),
    ("s17", "shop-021", "test", "hold", "cash"),
    ("s18", "shop-026", "test", "hold", "cash"),
    ("s19", "shop-004", "test", "not_now", ""),
    ("s20", "shop-006", "test", "not_now", ""),
    ("s21", "shop-025", "test", "not_now", ""),
    ("s22", "shop-008", "test", "not_now", ""),
    ("s23", "shop-030", "test", "not_now", "blurry"),
    ("s24", "shop-037", "test", "not_now", "dark"),
    # Stress photos: reported, not gated. Small, crowded, overlapping, tilted,
    # dim or a little soft; the limits of counting from one phone photo.
    ("t01", "shop-003", "stress", "support", "small"),
    ("t02", "shop-011", "stress", "support", "crowded"),
    ("t03", "shop-016", "stress", "support", "tilted"),
    ("t04", "shop-029", "stress", "support", "dim"),
    ("t05", "shop-038", "stress", "support", "soft"),
    ("t06", "shop-019", "stress", "support", "crowded"),
    # A clear photo for the scene's dark-then-clear conversation (not counted).
    ("x01", "shop-020", "scene", "hold", "cash"),
)

LABEL_WHY = {
    ("support", ""): "pays on time by bank transfer; nothing overdue",
    ("hold", "cash"): "pays on time, but in cash to the rep: nothing bank-verified to count yet",
    ("hold", "borrowed"): "good payer, but the shelf shows far more of one item than Coral "
                          "Lane delivered in 4 weeks",
    ("hold", "dark"): "pays on time, but in cash to the rep: nothing bank-verified to count yet",
    ("not_now", ""): "pays late or disputes; weeks overdue",
    ("not_now", "blurry"): "pays late or disputes; weeks overdue",
    ("not_now", "dark"): "pays late; broken promises",
    ("support", "small"): "pays on time by bank transfer; nothing overdue",
    ("support", "crowded"): "pays on time by bank transfer; nothing overdue",
    ("support", "tilted"): "pays on time by bank transfer; nothing overdue",
    ("support", "dim"): "pays on time by bank transfer; nothing overdue",
    ("support", "soft"): "pays on time by bank transfer; nothing overdue",
}


def delivered(world: Any, shop_id: str) -> tuple[dict[str, int], dict[str, int]]:
    """(pieces in 8 weeks, pieces in the last 28 days) per shelf item."""
    sku = {s.sku: s for s in world.skus}
    allp: dict[str, int] = {}
    recent: dict[str, int] = {}
    for d in world.deliveries:
        if d.shop_id != shop_id:
            continue
        for ln in d.lines:
            if ln.sku not in LOOKS:
                continue
            n = ln.quantity * pieces_per_unit(sku[ln.sku])
            allp[ln.sku] = allp.get(ln.sku, 0) + n
            if d.day >= world.live_day - 28:
                recent[ln.sku] = recent.get(ln.sku, 0) + n
    return allp, recent


def spec_for(cid: str, shop_id: str, twist: str, world: Any, rng: random.Random
             ) -> tuple[ShelfSpec, str]:
    allp, recent = delivered(world, shop_id)
    cands = sorted(allp)
    rng.shuffle(cands)
    n_rows = rng.choice((3, 3, 4)) if len(cands) >= 3 else rng.choice((2, 3))
    scale = rng.uniform(0.72, 1.0)
    picks = cands[: max(2, min(len(cands), n_rows + rng.randint(0, 1)))]
    borrowed = ""
    if twist == "borrowed":
        low = [s for s in cands if recent.get(s, 0) == 0]
        borrowed = low[0] if low else cands[-1]
        if borrowed not in picks:
            picks = [borrowed] + picks[: n_rows]
    runs: list[Run] = []
    for s in picks:
        cap = int(SHELF_SLACK * recent.get(s, 0) + SHELF_SLACK_PIECES)
        if s == borrowed:
            n = cap + rng.randint(6, 9)
        else:
            n = max(2, min(cap, rng.randint(3, 12)))
        stack = 2 if LOOKS[s].shape in ("can",) and n >= 10 and rng.random() < 0.6 else 1
        runs.append(Run(s, n if stack == 1 else (n + 1) // 2, stack))
    others = [k for k in LOOKS if k not in allp] + list(OTHER_LOOKS)
    if rng.random() < 0.75:
        runs.append(Run(rng.choice(others), rng.randint(2, 5)))
    rng.shuffle(runs)
    rows: list[list[Run]] = [[] for _ in range(n_rows)]
    for i, r in enumerate(runs):
        rows[i % n_rows].append(r)
    rows = [row for row in rows if row]
    quality = twist if twist in ("blurry", "dark") else "ok"
    spec = ShelfSpec(seed=rng.randint(1, 10**9), rows=tuple(tuple(r) for r in rows),
                     quality=quality, tilt_deg=round(rng.uniform(-3.0, 3.0), 2),
                     scale=round(scale, 3), light=round(rng.uniform(0.82, 1.1), 3),
                     wall=(rng.randint(190, 220), rng.randint(182, 206), rng.randint(160, 186)))
    stress = {"small": {"scale": 0.5}, "crowded": {"gap": (-16, -6)},
              "tilted": {"tilt_deg": 7.5}, "dim": {"light": 0.55},
              "soft": {"blur": 1.9}}.get(twist)
    if stress:
        from dataclasses import replace

        spec = replace(spec, **stress)  # type: ignore[arg-type]
    return spec, borrowed


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma-separated ids to draw; keep the others")
    only = {x for x in ap.parse_args(argv).only.split(",") if x}
    world = load_world()
    rng = random.Random(SEED)
    (OUT / "shelves").mkdir(parents=True, exist_ok=True)
    old = {}
    if only and (OUT / "shelves.jsonl").exists():
        old = {json.loads(x)["id"]: json.loads(x)
               for x in (OUT / "shelves.jsonl").read_text().splitlines() if x}
    lines = []
    for cid, shop_id, split, label, twist in CASES:
        allp, recent = delivered(world, shop_id)
        # Specs are drawn for every case so the seed sequence never shifts.
        spec, borrowed = spec_for(cid, shop_id, twist, world, rng)
        if only and cid not in only and cid in old:
            lines.append(old[cid])
            continue
        data, truth = render(spec)
        name = f"{cid}.jpg"
        (OUT / "shelves" / name).write_bytes(data)
        counted = {k: v for k, v in sorted(truth.items()) if k in allp}
        if borrowed:
            cap = SHELF_SLACK * recent.get(borrowed, 0) + SHELF_SLACK_PIECES
            assert counted.get(borrowed, 0) > cap, (cid, borrowed, counted, cap)
        shop = world.shop(shop_id)
        lines.append({
            "id": cid, "split": split, "shop_id": shop_id, "shop_name": shop.name,
            "file": f"shelves/{name}", "sha256": hashlib.sha256(data).hexdigest(),
            "quality": spec.quality, "countable": spec.quality == "ok" and split != "scene",
            "truth": dict(sorted(truth.items())), "truth_counted": counted,
            "other_pieces": sum(v for k, v in truth.items() if k not in allp),
            "label": label, "twist": twist,
            "label_why": LABEL_WHY.get((label, twist), LABEL_WHY.get((label, ""), "")),
            "borrowed_sku": borrowed, "synthetic": True,
            # Provenance (RAI-261): drawn by code, never a real shop photo.
            "source": "synthetic",
        })
        print(cid, shop_id, split, label, twist, counted, "other", lines[-1]["other_pieces"])
    (OUT / "shelves.jsonl").write_text(
        "".join(json.dumps(x, sort_keys=True) + "\n" for x in lines), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
