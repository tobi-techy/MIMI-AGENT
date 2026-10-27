"""The router's distilled fast classifier: naive Bayes over character n-grams.

Muse Spark takes about a second per routing call, and the routing budget is
800 ms at p95. So, as frontier routers do, the big model's job is distilled
into a small one: Muse Spark (contributor tier, synthetic prompts only) wrote
about 2,700 labelled shop, rep and owner messages across the skill catalogue
in English, Pidgin, Yoruba, Hausa and Igbo (``scripts/make_router_train.py``;
nothing from the eval set is shown to it, and any eval match is dropped).
A multinomial naive Bayes model over character 3-5 grams and words is
trained from that file at first use (deterministic, well under a second) and
classifies a message in about a millisecond.

The router trusts it only above a per-class probability tuned on the eval's
``dev`` split, and never for a money skill without a phrase rule pointing the
same way; everything else still goes to the model. Pure Python, no new
dependency.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from mimi.harness.lexicon import normalise

DATA = Path(__file__).resolve().parent / "data" / "router_train.jsonl"
ALPHA = 0.3


def features(text: str) -> Counter[str]:
    t = normalise(text)
    t = re.sub(r"\d+", "0", t)
    words = re.findall(r"[a-z0']+|0|[^\sa-z0-9]", t)
    feats: Counter[str] = Counter(f"w:{w}" for w in words)
    feats.update(f"b:{a}_{b}" for a, b in zip(words, words[1:], strict=False))
    padded = f" {' '.join(words)} "
    for n in (3, 4, 5):
        for i in range(len(padded) - n + 1):
            g = padded[i:i + n]
            if g.strip():
                feats[f"c:{g}"] += 1
    return feats


@dataclass
class NaiveBayes:
    classes: list[str] = field(default_factory=list)
    prior: dict[str, float] = field(default_factory=dict)
    loglik: dict[str, dict[str, float]] = field(default_factory=dict)
    unseen: dict[str, float] = field(default_factory=dict)
    vocab: set[str] = field(default_factory=set)
    rows: int = 0

    @classmethod
    def train(cls, rows: Iterable[tuple[str, str]]) -> NaiveBayes:
        counts: dict[str, Counter[str]] = defaultdict(Counter)
        docs: Counter[str] = Counter()
        n = 0
        for text, label in rows:
            counts[label].update(features(text))
            docs[label] += 1
            n += 1
        vocab = set().union(*counts.values()) if counts else set()
        v = len(vocab)
        nb = cls(classes=sorted(counts), vocab=vocab, rows=n)
        for c in nb.classes:
            total = sum(counts[c].values())
            denom = total + ALPHA * v
            nb.prior[c] = math.log(docs[c] / n)
            nb.loglik[c] = {f: math.log((k + ALPHA) / denom) for f, k in counts[c].items()}
            nb.unseen[c] = math.log(ALPHA / denom)
        return nb

    def predict(self, text: str, allowed: Iterable[str] | None = None
                ) -> list[tuple[str, float]]:
        """Classes with posterior probabilities, best first."""
        feats = {f: k for f, k in features(text).items() if f in self.vocab}
        classes = [c for c in self.classes if allowed is None or c in set(allowed)]
        if not classes or not feats:
            return []
        scores = {}
        for c in classes:
            ll, un = self.loglik[c], self.unseen[c]
            scores[c] = self.prior[c] + sum(k * ll.get(f, un) for f, k in feats.items())
        top = max(scores.values())
        z = sum(math.exp(s - top) for s in scores.values())
        out = [(c, math.exp(s - top) / z) for c, s in scores.items()]
        return sorted(out, key=lambda kv: (-kv[1], kv[0]))


_MODEL: NaiveBayes | None = None
_LOCK = threading.Lock()


def model(path: Path = DATA) -> NaiveBayes:
    global _MODEL
    with _LOCK:
        if _MODEL is None:
            rows = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    rows.append((r["text"], r["skill"]))
            _MODEL = NaiveBayes.train(rows)
        return _MODEL
