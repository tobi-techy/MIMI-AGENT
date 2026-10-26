"""Voice-note eval: speech to text, then the same receipt reader and guards.

    python -m mimi.voice.eval                 # replay recorded answers (CI)
    python -m mimi.voice.eval --live          # call Spitch, Muse Voice Transcribe
                                              # and Muse Spark; resume-safe
    python -m mimi.voice.eval --live --fresh  # all calls new
    python -m mimi.voice.eval --fake          # fake transcriber + keyword judge

Clips: ``eval/voice/clips.jsonl`` (16 synthetic-generated voice notes in
English, Pidgin, Yoruba, Igbo, Hausa and Yoruba-English, some with market
noise; see ``scripts/make_voice_fixtures.py``). Each clip is run through
three transcribers: ``spitch``, ``muse`` (muse-voice-transcribe-1.0) and the
``router`` (the production path, ``router.py``). Each transcript is stored as
a voice reply and judged end to end by Muse Spark (``ReceiptJudge``) and the
code guards (``decide``), exactly as in production. A clip whose transcript
failed is not judged: production asks the shop to type, which is counted as
safe but not correct.

Metrics, per transcriber and per language:

* WER against the text the clip was generated from (corpus WER: all errors
  over all reference words), plain and with tone marks folded (``wer.py``);
* verdict accuracy: the outcome is the label or an accepted alternative;
* false confirms: consent recorded for a clip that is not a confirmation
  (pass mark 0, for every transcriber);
* disputes caught: dispute and partial clips that end in an owner dispute
  flag on the first read (pass mark 100%, for the router);
* disputes never confirmed: dispute and partial clips that do not end in
  consent (a clip read as unclear gets one clarifying message and then an
  owner flag; it can never be confirmed). Must be 100% for every transcriber;
* confirms recorded: confirmation clips that end in consent (reported);
* transcription latency, audio seconds and Muse Spark tokens.

WER has no pass mark; it is reported as measured.

Exit code: 1 when a safety mark fails (any false confirm, or any dispute
that ends in consent). The first-read dispute mark is reported in the
summary and the report's ``passes``; ``--strict`` makes it fail the run too.

Recorded answers: ``eval/voice/transcripts.<provider>.cache.json`` (text
only, never audio) and ``eval/voice/verdicts.cache.json`` (Muse Spark).
``--live`` writes ``eval/voice/voice.report.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
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
from mimi.receipts.receipt import receipt_keywords, receipt_text
from mimi.receipts.verdict import ReceiptJudge, fake_verdict
from mimi.voice.audio import inspect
from mimi.voice.base import Transcriber, Transcript, VoiceContext
from mimi.voice.cache import CachedTranscriber
from mimi.voice.env import live_transcribers
from mimi.voice.fake import FakeTranscriber, audio_sha256
from mimi.voice.fixtures import VOICE_DIR, clip_bytes, load_clips
from mimi.voice.router import DEFAULT_POLICY, VoiceRouter
from mimi.voice.wer import corpus_wer, wer
from mimi.world import load_world
from mimi.world.model import World

VERDICT_CACHE = VOICE_DIR / "verdicts.cache.json"
REPORT = VOICE_DIR / "voice.report.json"
WAT = timezone(timedelta(hours=1))
PROVIDERS = ("spitch", "muse")


def transcript_cache(provider: str) -> Path:
    return VOICE_DIR / f"transcripts.{provider}.cache.json"


@dataclass(frozen=True)
class ClipResult:
    clip: dict[str, Any]
    transcriber: str
    transcript: Transcript
    outcome: str
    action: str
    confidence: float
    held_back: str
    prompt_tokens: int
    completion_tokens: int

    @property
    def acceptable(self) -> set[str]:
        return {self.clip["expected"], *self.clip.get("also_ok", [])}

    @property
    def correct(self) -> bool:
        return self.transcript.status != "failed" and self.outcome in self.acceptable

    @property
    def wer(self) -> float:
        return wer(self.clip["reference"], self.transcript.text)


def voice_reply(clip: dict[str, Any], t: Transcript, data: bytes) -> ShopReply:
    at = datetime(2026, 10, 26, 14, 0, tzinfo=WAT)
    return ShopReply(
        message_id=f"wamid.{clip['id']}",
        job_id=f"rcpt-{clip['delivery_id']}",
        shop_id="eval-shop",
        sender="+234-eval",
        text=t.text if t.status != "failed" else "",
        received_at=at.isoformat(),
        kind="voice",
        transcript_status=t.status,
        transcript_provider=t.provider,
        transcript_confidence=t.confidence,
        audio_seconds=t.duration_s,
        audio_sha256=audio_sha256(data),
    )


def run_clip(
    name: str,
    transcriber: Transcriber,
    judge: ReceiptJudge,
    world: World,
    clip: dict[str, Any],
) -> ClipResult:
    data = clip_bytes(clip)
    audio = inspect(data)
    delivery = world.delivery(clip["delivery_id"])
    ctx = VoiceContext(language_hint=clip["hint"], keywords=receipt_keywords(delivery))
    t = transcriber.transcribe(audio, ctx)
    if t.status == "failed":
        return ClipResult(clip, name, t, "no_transcript", "type_request", 0.0, "", 0, 0)
    reply = voice_reply(clip, t, data)
    shop = world.shop(delivery.shop_id)
    mimi_sent = [receipt_text(delivery, shop.name, world.distributor)]
    judged = judge(delivery=delivery, earlier=[], new=[reply], mimi_sent=mimi_sent)
    d = decide(
        judged.verdict,
        delivery=delivery,
        replies=[reply],
        clarified=False,
        new_replies=[reply],
    )
    return ClipResult(
        clip,
        name,
        t,
        judged.verdict.outcome,
        d.action,
        judged.verdict.confidence,
        d.held_back,
        judged.prompt_tokens,
        judged.completion_tokens,
    )


def _rate(n: int, d: int) -> float:
    return round(n / d, 4) if d else 1.0


def _is_dispute(r: ClipResult) -> bool:
    return r.clip["expected"] in ("disputed", "partial")


def summarise(results: Sequence[ClipResult]) -> dict[str, Any]:
    disputes = [r for r in results if _is_dispute(r)]
    confirms = [r for r in results if r.clip["expected"] == "confirmed"]
    false_confirms = [
        r for r in results if r.action == "consent" and "confirmed" not in r.acceptable
    ]
    lat = [r.transcript.latency_s for r in results if r.transcript.latency_s]
    pairs = [(r.clip["reference"], r.transcript.text) for r in results]
    return {
        "clips": len(results),
        "wer": round(corpus_wer(pairs), 4),
        "wer_folded": round(corpus_wer(pairs, folded=True), 4),
        "verdict_accuracy": _rate(sum(r.correct for r in results), len(results)),
        "false_confirms": len(false_confirms),
        "false_confirm_ids": [r.clip["id"] for r in false_confirms],
        "disputes_caught": _rate(sum(r.action == "flag_dispute" for r in disputes), len(disputes)),
        "disputes_n": len(disputes),
        "disputes_never_confirmed": _rate(
            sum(r.action != "consent" for r in disputes), len(disputes)
        ),
        "missed_dispute_ids": [r.clip["id"] for r in disputes if r.action != "flag_dispute"],
        "confirms_recorded": _rate(sum(r.action == "consent" for r in confirms), len(confirms)),
        "confirms_n": len(confirms),
        "transcripts_failed": sum(r.transcript.status == "failed" for r in results),
        "transcripts_low": sum(r.transcript.status == "low" for r in results),
        "latency_mean_s": round(sum(lat) / len(lat), 3) if lat else 0.0,
        "latency_max_s": round(max(lat), 3) if lat else 0.0,
        "audio_seconds": round(sum(r.transcript.duration_s or 0.0 for r in results), 2),
        "judge_prompt_tokens": sum(r.prompt_tokens for r in results),
        "judge_completion_tokens": sum(r.completion_tokens for r in results),
    }


def score(by_name: Mapping[str, Sequence[ClipResult]]) -> dict[str, Any]:
    report: dict[str, Any] = {"transcribers": {}, "by_language": {}, "clips": []}
    for name, results in by_name.items():
        report["transcribers"][name] = summarise(results)
        by_lang: dict[str, list[ClipResult]] = defaultdict(list)
        for r in results:
            by_lang[r.clip["lang"]].append(r)
        report["by_language"][name] = {
            lang: {
                k: v
                for k, v in summarise(rs).items()
                if k
                in (
                    "clips",
                    "wer",
                    "wer_folded",
                    "verdict_accuracy",
                    "false_confirms",
                    "disputes_caught",
                    "disputes_n",
                )
            }
            for lang, rs in sorted(by_lang.items())
        }
    first = next(iter(by_name.values()), [])
    for i, clip_r in enumerate(first):
        row: dict[str, Any] = {
            "id": clip_r.clip["id"],
            "lang": clip_r.clip["lang"],
            "noise": clip_r.clip["noise"],
            "reference": clip_r.clip["reference"],
            "expected": clip_r.clip["expected"],
        }
        for name, results in by_name.items():
            r = results[i]
            row[name] = {
                "transcript": r.transcript.text,
                "status": r.transcript.status,
                "tried": list(r.transcript.tried),
                "wer": round(r.wer, 3),
                "outcome": r.outcome,
                "action": r.action,
                "held_back": r.held_back,
            }
        report["clips"].append(row)
    gate: dict[str, Any] = report["transcribers"].get("router") or next(iter(report["transcribers"].values()), {})
    report["passes"] = {
        "false_confirms_all_transcribers": all(
            s["false_confirms"] == 0 for s in report["transcribers"].values()
        ),
        "disputes_never_confirmed_all_transcribers": all(
            s["disputes_never_confirmed"] >= 1.0 for s in report["transcribers"].values()
        ),
        "router_disputes_caught_first_read": gate.get("disputes_caught", 0) >= 1.0,
    }
    report["pass_safety"] = (
        report["passes"]["false_confirms_all_transcribers"]
        and report["passes"]["disputes_never_confirmed_all_transcribers"]
    )
    report["pass"] = all(report["passes"].values())
    return report


def run(
    transcribers: Mapping[str, Transcriber],
    model: ChatModel,
    clips: Sequence[dict[str, Any]],
    workers: int = 4,
) -> dict[str, list[ClipResult]]:
    world = load_world()
    judge = ReceiptJudge(model)
    out: dict[str, list[ClipResult]] = {}
    for name, t in transcribers.items():
        if workers <= 1:
            out[name] = [run_clip(name, t, judge, world, c) for c in clips]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                out[name] = list(
                    pool.map(lambda c, n=name, tr=t: run_clip(n, tr, judge, world, c), clips)
                )
    return out


def table(report: dict[str, Any]) -> str:
    """The side-by-side table (Markdown), per language and overall."""
    names = list(report["transcribers"])
    head = "| Language | " + " | ".join(f"{n} WER | {n} verdicts" for n in names) + " |"
    sep = "|---|" + "---|---|" * len(names)
    langs = sorted({lang for n in names for lang in report["by_language"][n]})
    rows = []
    for lang in [*langs, "all"]:
        cells = []
        for n in names:
            s = report["transcribers"][n] if lang == "all" else report["by_language"][n][lang]
            ok = round(s["verdict_accuracy"] * s["clips"])
            cells.append(f"{s['wer']:.0%} | {ok}/{s['clips']}")
        label = "**all**" if lang == "all" else f"{lang} (n={s['clips']})"
        rows.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join([head, sep, *rows])


def _print(report: dict[str, Any], label: str) -> None:
    print(f"voice eval: {len(report['clips'])} synthetic clips, {label}")
    for name, s in report["transcribers"].items():
        print(
            f"  {name:<7} WER {s['wer']:.1%} (folded {s['wer_folded']:.1%})  "
            f"verdicts {s['verdict_accuracy']:.1%}  false confirms {s['false_confirms']}  "
            f"disputes caught {s['disputes_caught']:.0%} (n={s['disputes_n']})  "
            f"confirms recorded {s['confirms_recorded']:.0%}  failed {s['transcripts_failed']} "
            f"low {s['transcripts_low']}  latency {s['latency_mean_s']:.2f}s mean"
        )
    print(table(report))
    for row in report["clips"]:
        for name in report["transcribers"]:
            r = row[name]
            if (
                r["outcome"] != row["expected"]
                and not (
                    row["expected"] in ("disputed", "partial") and r["action"] == "flag_dispute"
                )
                and not (row["expected"] == "unrelated" and r["outcome"] == "unclear")
            ):
                print(
                    f"  {name} {row['id']} want {row['expected']} got {r['outcome']} "
                    f"({r['action']}) heard: {r['transcript']!r}"
                )
    if report["pass"]:
        print("PASS")
    elif report["pass_safety"]:
        print(f"SAFETY PASS; pass mark not met: {report['passes']}")
    else:
        print(f"FAIL {report['passes']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.voice.eval")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--fake", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="with --live: clear the caches")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="fail on every pass mark")
    args = ap.parse_args(argv)

    clips = list(load_clips())
    providers: dict[str, Transcriber]
    model: Any
    if args.fake:
        fake = FakeTranscriber()
        providers = {"fake": fake}
        model, label = FakeModel(fake_verdict, name="fake:mimi"), "fake transcriber and judge"
    elif args.live:
        live = live_transcribers()
        missing = [p for p in PROVIDERS if p not in live]
        if missing or not has_key():
            print(
                f"--live needs SPITCH_API_KEY and META_API_KEY (missing {missing})", file=sys.stderr
            )
            return 2
        if args.fresh:
            for p in [*(transcript_cache(n) for n in PROVIDERS), VERDICT_CACHE]:
                p.unlink(missing_ok=True)
        providers = {
            n: CachedTranscriber(live[n], transcript_cache(n), mode="through") for n in PROVIDERS
        }
        model = CachedModel(from_env("mimi"), VERDICT_CACHE, mode="through")
        label = f"live, judge {model.name}"
    else:
        providers = {
            n: CachedTranscriber(None, transcript_cache(n), mode="replay", name=n)
            for n in PROVIDERS
        }
        name = os.environ.get("MIMI_MODEL", "").strip() or DEFAULT_MODELS["mimi"]
        model = CachedModel(None, VERDICT_CACHE, mode="replay", name=name)
        label = f"replayed from cache, judge {name}"
    transcribers: dict[str, Transcriber] = dict(providers)
    if not args.fake:
        transcribers["router"] = VoiceRouter(providers, DEFAULT_POLICY)
    started = time.monotonic()
    results = run(transcribers, model, clips, workers=1 if args.fake else args.workers)
    report = score(results)
    report["mode"] = label
    report["wall_seconds"] = round(time.monotonic() - started, 1)
    if isinstance(model, CachedModel):
        report["judge_live_calls_this_pass"] = model.live_calls
        report["judge_replayed_calls"] = model.cached_calls
    report["transcriber_live_calls_this_pass"] = {
        n: getattr(t, "live_calls", 0) for n, t in providers.items()
    }
    if args.live:
        report["recorded_at"] = datetime.now(WAT).isoformat(timespec="minutes")
        report["table_markdown"] = table(report)
        REPORT.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    if args.json:
        print(json.dumps(report, indent=1, ensure_ascii=False))
    else:
        _print(report, label)
    if args.fake:
        return 0
    ok = report["pass"] if args.strict else report["pass_safety"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
