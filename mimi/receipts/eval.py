"""Eval for the receipt reply reader (RAI-267).

    python -m mimi.receipts.eval            # replay the cache
    python -m mimi.receipts.eval --live     # call Meta
    python -m mimi.receipts.eval --live --fresh
    python -m mimi.receipts.eval --fake     # FakeModel

Cases: ``eval/receipts/receipt_replies.jsonl``, synthetic and hand-written
(English, Pidgin, Yoruba, Igbo, Hausa, mixed, emoji, "ok", late replies,
partial disputes, sarcasm and ambiguity, voice notes, typos, injection).

* ``--live`` calls ``MIMI_MODEL`` on Meta through the cache in "through" mode:
  each answer is saved to ``receipt_replies.cache.json`` as it lands, so a run
  cut short resumes where it stopped. ``--fresh`` deletes the cache first, so
  every case is a new live call. The report is written to
  ``receipt_replies.report.json``.
* The default replays the cache only (what CI runs): no network, and a case
  with no recorded answer is an error, never a silent "unclear".
* ``--fake`` runs the keyword FakeModel, for plumbing only; it is not meant to
  pass.

Each case is judged and then put through the same code guards as production
(``decide``). Metrics:

* overall: the model's outcome is the label or one of ``also_ok``;
* per outcome: the same, by labelled outcome;
* false confirms: consent would be recorded for a case whose acceptable
  outcomes do not include ``confirmed`` (pass mark: 0);
* confirmed recall: clear confirmations (label ``confirmed``, no
  ``also_ok``) that end in consent after the guards (pass mark: 95%).
  Ambiguous ones (a lone ✅) are reported separately;
* disputed recall: clear disputes (label ``disputed`` or ``partial`` and
  ``also_ok`` only disputed or partial) that end in an owner dispute flag
  (pass mark: 100%). Ambiguous disputes ("No") are reported separately; they
  may get one clarifying message first, and can never end in consent (that
  would be a false confirm);
* disputed line match: of the labelled disputed lines, how many the
  resolved flag names (reported, not gated). A label may be a list of lines
  when the shop's words fit more than one ("noodles" on a receipt with two
  noodle lines).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mimi.models import FakeModel, from_env, has_key
from mimi.models.base import ChatModel
from mimi.models.cache import CachedModel
from mimi.models.env import DEFAULT_MODELS
from mimi.receipts.decide import decide
from mimi.receipts.inbox import ShopReply
from mimi.receipts.receipt import clarify_text, receipt_text
from mimi.receipts.verdict import ReceiptJudge, fake_verdict
from mimi.world import load_world
from mimi.world.model import World

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval" / "receipts"
CASES = EVAL_DIR / "receipt_replies.jsonl"
CACHE = EVAL_DIR / "receipt_replies.cache.json"
REPORT = EVAL_DIR / "receipt_replies.report.json"
WAT = timezone(timedelta(hours=1))

PASS_MARKS: dict[str, float] = {
    "false_confirms": 0,
    "confirmed_recall": 0.95,
    "disputed_recall": 1.0,
    "overall": 0.92,
}


@dataclass(frozen=True)
class CaseResult:
    case: dict[str, Any]
    outcome: str
    action: str
    confidence: float
    language: str
    via: str
    evidence_found: bool
    held_back: str
    lines: tuple[int, ...]
    prompt_tokens: int
    completion_tokens: int

    @property
    def acceptable(self) -> set[str]:
        return {self.case["expected"], *self.case.get("also_ok", [])}

    @property
    def correct(self) -> bool:
        return self.outcome in self.acceptable


def load_cases(path: Path = CASES) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _replies(case: dict[str, Any]) -> tuple[list[ShopReply], list[ShopReply]]:
    base = datetime(2026, 10, 26, 14, 0, tzinfo=WAT)
    out: list[ShopReply] = []
    texts = [*case["earlier"], *case["new"]]
    for i, text in enumerate(texts):
        # Earlier messages sit hours before; a burst is seconds apart.
        at = base + (
            timedelta(hours=i)
            if i < len(case["earlier"])
            else timedelta(hours=len(case["earlier"]) * 12, seconds=30 * i)
        )
        out.append(
            ShopReply(
                message_id=f"wamid.{case['id']}.{i}",
                job_id=f"rcpt-{case['delivery_id']}",
                shop_id="eval-shop",
                sender="+234-eval",
                text=text,
                received_at=at.isoformat(),
            )
        )
    n = len(case["earlier"])
    return out[:n], out[n:]


def run_case(judge: ReceiptJudge, world: World, case: dict[str, Any]) -> CaseResult:
    delivery = world.delivery(case["delivery_id"])
    shop = world.shop(delivery.shop_id)
    earlier, new = _replies(case)
    mimi_sent = [receipt_text(delivery, shop.name, world.distributor)]
    if case.get("clarified"):
        mimi_sent.append(clarify_text(delivery))
    judged = judge(delivery=delivery, earlier=earlier, new=new, mimi_sent=mimi_sent)
    d = decide(
        judged.verdict,
        delivery=delivery,
        replies=[*earlier, *new],
        clarified=bool(case.get("clarified")),
    )
    return CaseResult(
        case=case,
        outcome=judged.verdict.outcome,
        action=d.action,
        confidence=judged.verdict.confidence,
        language=judged.verdict.language,
        via=judged.via,
        evidence_found=d.evidence_found,
        held_back=d.held_back,
        lines=tuple(x.line_no for x in d.disputed_lines if x.line_no is not None),
        prompt_tokens=judged.prompt_tokens,
        completion_tokens=judged.completion_tokens,
    )


def _rate(n: int, d: int) -> float:
    return round(n / d, 4) if d else 1.0


def _line_hit(want: int | list[int], got: tuple[int, ...]) -> bool:
    """A labelled line is an int, or a list of lines any of which is right
    (the shop's words fit more than one receipt line)."""
    return any(x in got for x in want) if isinstance(want, list) else want in got


def score(results: Sequence[CaseResult]) -> dict[str, Any]:
    by_outcome: dict[str, list[CaseResult]] = defaultdict(list)
    by_lang: dict[str, list[CaseResult]] = defaultdict(list)
    by_cat: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        by_outcome[r.case["expected"]].append(r)
        by_lang[r.case["lang"]].append(r)
        by_cat[r.case["category"]].append(r)
    false_confirms = [
        r for r in results if r.action == "consent" and "confirmed" not in r.acceptable
    ]
    clear_yes = [
        r
        for r in results
        if r.case["expected"] == "confirmed" and not r.case["also_ok"]
    ]
    all_yes = [r for r in results if r.case["expected"] == "confirmed"]
    all_disputes = [r for r in results if r.case["expected"] in ("disputed", "partial")]
    # Clear disputes: every acceptable outcome is a dispute. "No" alone or
    # "which delivery?" may also be read as unclear: those get a clarifying
    # message and then an owner flag, never consent.
    disputes = [
        r for r in all_disputes if set(r.case["also_ok"]) <= {"disputed", "partial"}
    ]
    want_lines = [(r, n) for r in all_disputes for n in r.case.get("expect_lines", [])]
    report: dict[str, Any] = {
        "cases": len(results),
        "overall": _rate(sum(r.correct for r in results), len(results)),
        "per_outcome": {
            k: {"n": len(v), "accuracy": _rate(sum(r.correct for r in v), len(v))}
            for k, v in sorted(by_outcome.items())
        },
        "false_confirms": len(false_confirms),
        "false_confirm_rate": _rate(
            len(false_confirms),
            sum(1 for r in results if "confirmed" not in r.acceptable),
        ),
        "false_confirm_ids": [r.case["id"] for r in false_confirms],
        "confirmed_recall": _rate(
            sum(r.action == "consent" for r in clear_yes), len(clear_yes)
        ),
        "confirmed_recall_n": len(clear_yes),
        "confirmed_recall_incl_ambiguous": _rate(
            sum(r.action == "consent" for r in all_yes), len(all_yes)
        ),
        "disputed_recall": _rate(
            sum(r.action == "flag_dispute" for r in disputes), len(disputes)
        ),
        "disputed_recall_n": len(disputes),
        "disputed_recall_incl_ambiguous": _rate(
            sum(r.action == "flag_dispute" for r in all_disputes), len(all_disputes)
        ),
        "ambiguous_disputes_consented": sum(
            r.action == "consent" for r in all_disputes
        ),
        "disputed_line_match": _rate(
            sum(_line_hit(n, r.lines) for r, n in want_lines), len(want_lines)
        ),
        "language_breakdown": {
            k: {
                "n": len(v),
                "accuracy": _rate(sum(r.correct for r in v), len(v)),
                "detected": dict(
                    sorted(
                        {
                            lang: sum(1 for r in v if r.language == lang)
                            for lang in {r.language for r in v}
                        }.items()
                    )
                ),
            }
            for k, v in sorted(by_lang.items())
        },
        "category_breakdown": {
            k: {"n": len(v), "accuracy": _rate(sum(r.correct for r in v), len(v))}
            for k, v in sorted(by_cat.items())
        },
        "repairs": sum(1 for r in results if r.via == "repair"),
        "unparseable_or_error": sum(
            1 for r in results if r.via in ("unparseable", "model_error")
        ),
        "held_back_confirms": {
            h: sum(1 for r in results if r.held_back == h)
            for h in sorted({r.held_back for r in results if r.held_back})
        },
        "prompt_tokens": sum(r.prompt_tokens for r in results),
        "completion_tokens": sum(r.completion_tokens for r in results),
        "misses": [
            {
                "id": r.case["id"],
                "text": r.case["new"],
                "expected": r.case["expected"],
                "also_ok": r.case["also_ok"],
                "got": r.outcome,
                "confidence": r.confidence,
                "action": r.action,
            }
            for r in results
            if not r.correct
        ],
    }
    report["passes"] = {
        "false_confirms": report["false_confirms"] <= PASS_MARKS["false_confirms"],
        "confirmed_recall": report["confirmed_recall"]
        >= PASS_MARKS["confirmed_recall"],
        "disputed_recall": report["disputed_recall"] >= PASS_MARKS["disputed_recall"],
        "overall": report["overall"] >= PASS_MARKS["overall"],
    }
    report["pass"] = all(report["passes"].values())
    return report


def run(
    model: ChatModel, cases: Sequence[dict[str, Any]], workers: int = 1
) -> list[CaseResult]:
    world = load_world()
    judge = ReceiptJudge(model)
    if workers <= 1:
        return [run_case(judge, world, c) for c in cases]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda c: run_case(judge, world, c), cases))


def _print(report: dict[str, Any], label: str) -> None:
    print(f"receipt reply eval: {report['cases']} cases, model {label}")
    print(f"  overall           {report['overall']:.1%}")
    for k, v in report["per_outcome"].items():
        print(f"  {k:<17} {v['accuracy']:.1%}  (n={v['n']})")
    print(
        f"  false confirms    {report['false_confirms']} "
        f"(rate {report['false_confirm_rate']:.1%})"
    )
    print(
        f"  confirmed recall  {report['confirmed_recall']:.1%} "
        f"(n={report['confirmed_recall_n']}; incl. ambiguous "
        f"{report['confirmed_recall_incl_ambiguous']:.1%})"
    )
    print(
        f"  disputed recall   {report['disputed_recall']:.1%} "
        f"(n={report['disputed_recall_n']}; incl. ambiguous "
        f"{report['disputed_recall_incl_ambiguous']:.1%}); line match "
        f"{report['disputed_line_match']:.1%}"
    )
    print("  languages:")
    for k, v in report["language_breakdown"].items():
        print(f"    {k:<8} {v['accuracy']:.1%} (n={v['n']}) detected {v['detected']}")
    print(f"  tokens {report['prompt_tokens']} in / {report['completion_tokens']} out")
    for m in report["misses"]:
        print(f"  MISS {m['id']} {m['text']} want {m['expected']} got {m['got']}")
    print("PASS" if report["pass"] else f"FAIL {report['passes']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.receipts.eval")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--fake", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="with --live: clear cache")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--cases", default=str(CASES))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cases = load_cases(Path(args.cases))
    model: Any
    if args.fake:
        model, label = FakeModel(fake_verdict, name="fake:mimi"), "fake"
    elif args.live:
        if not has_key():
            print("META_API_KEY is not set; --live needs it", file=sys.stderr)
            return 2
        if args.fresh and CACHE.exists():
            CACHE.unlink()
        model = CachedModel(from_env("mimi"), CACHE, mode="through")
        label = f"{model.name} (live)"
    else:
        import os

        name = os.environ.get("MIMI_MODEL", "").strip() or DEFAULT_MODELS["mimi"]
        model = CachedModel(None, CACHE, mode="replay", name=name)
        label = f"{name} (replayed from cache)"
    started = time.monotonic()
    results = run(model, cases, workers=1 if args.fake else args.workers)
    report = score(results)
    report["model"] = label
    report["wall_seconds"] = round(time.monotonic() - started, 1)
    if isinstance(model, CachedModel):
        report["live_calls_this_pass"] = model.live_calls
        report["replayed_calls"] = model.cached_calls
    if args.live:
        report["recorded_at"] = datetime.now(WAT).isoformat(timespec="minutes")
        REPORT.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    if args.json:
        print(json.dumps(report, indent=1, ensure_ascii=False))
    else:
        _print(report, label)
    return 0 if report["pass"] or args.fake else 1


if __name__ == "__main__":
    sys.exit(main())
