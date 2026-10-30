"""Eval-set provenance (RAI-261, RAI-262).

Every payment-screenshot and shelf-photo label says where its file came
from, every image matches the SHA-256 on its label, and each label file
matches the set hash recorded in the README, so a set cannot change quietly.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETS = ["eval/proof/screens.jsonl", "eval/credit/shelves.jsonl"]
SOURCES = {"synthetic", "real-with-consent"}


def _rows(rel: str) -> list[dict[str, object]]:
    text = (ROOT / rel).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


@pytest.mark.parametrize("rel", SETS)
def test_every_label_has_a_source_and_its_image_matches(rel: str) -> None:
    rows = _rows(rel)
    assert rows
    base = (ROOT / rel).parent
    for r in rows:
        assert r.get("source") in SOURCES, (rel, r.get("id"))
        data = (base / str(r["file"])).read_bytes()
        assert hashlib.sha256(data).hexdigest() == r["sha256"], (rel, r.get("id"))


def test_shelves_source_agrees_with_the_synthetic_flag() -> None:
    for r in _rows("eval/credit/shelves.jsonl"):
        assert (r["source"] == "synthetic") == bool(r["synthetic"]), r["id"]


@pytest.mark.parametrize("rel", SETS)
def test_set_hash_matches_the_readme(rel: str) -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"\| `" + re.escape(rel) + r"` \|[^\n]*?`([0-9a-f]{64})` \|", readme)
    assert m, f"no set hash for {rel} in README.md"
    digest = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
    assert digest == m.group(1), f"{rel} changed: update its set hash in README.md"
