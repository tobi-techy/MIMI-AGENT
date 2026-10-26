"""Voice-note eval: speech to text, then the production receipt flow.

    python -m mimi.voice.eval                 # replay recorded answers (CI)
    python -m mimi.voice.eval --live          # call Spitch, Muse Voice Transcribe
                                              # and Muse Spark; resume-safe
    python -m mimi.voice.eval --live --fresh  # all calls new
    python -m mimi.voice.eval --fake          # fake transcriber + keyword judge
    python -m mimi.voice.eval --hints on      # only with language hints (default: both)
    python -m mimi.voice.eval --strict        # also fail on the first-read dispute mark

Clips: ``eval/voice/clips.jsonl`` (50 synthetic-generated voice notes in
English, Pidgin, Yoruba, Igbo, Hausa and code-switched mixes, with market
noise; see ``scripts/make_voice_fixtures.py``): confirms, disputes, partial
deliveries, payment promises with amounts and days, unclear and unrelated
replies. Each clip is run through three transcribers, ``spitch``, ``muse``
(muse-voice-transcribe-1.0) and the ``router`` (the production path,
``router.py``), once with the shop's language as a hint and once with none
(``spitch/no-hint``, ...). Each transcript goes through the production
``ReceiptFlow``: Muse Spark's verdict (``ReceiptJudge``), the code guards
and the payment-promise read-back. When Mimi reads a promise back, a
truthful shop answers (YES if the read-back is right, "No" if not) and the
flow runs again. A clip whose transcript failed is not judged: production
asks the shop to type, which is counted as safe but not correct.

Metrics, per transcriber and hint mode, and per language:

* WER against the text the clip was generated from (corpus WER), plain and
  with tone marks folded (``wer.py``);
* verdict accuracy, false confirms, disputes caught on the first read,
  disputes never confirmed, confirms recorded (as before);
* promises: amount and day exactly right on the clips with a clear promise;
  readings right (promise, unclear, paid claim) on every clip that talks
  about paying; promises heard where there was none; read-backs, questions,
  promises held until the delivery settles; recorded right after the
  shop's answer; **recorded without the shop's YES** and **recorded wrong**;
* latency, audio seconds, Muse Spark tokens, and the cost of recording
  (``cost``: from the caches and the fixtures).

Exit code 1 when a safety mark fails: any false confirm, any dispute that
ends in consent, or any promise recorded without the shop's YES or with the
wrong amount or day. The first-read dispute mark is a warning in the summary
and ``passes`` (Tobi's call, 26 Oct 2026); ``--strict`` makes it fail too.

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
from mimi.receipts.flow import HELPER, ReceiptFlow
from mimi.receipts.inbox import InMemoryShopInbox, ShopReply
from mimi.receipts.intents import RecordPaymentPromise, SendShopMessage
from mimi.receipts.promises import reading_of
from mimi.receipts.receipt import receipt_keywords
from mimi.receipts.verdict import ReceiptJudge, fake_verdict
from mimi.runtime.steps import StepContext, StepOutcome
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
    hinted: bool = True
    # The payment promise as read from the transcript (``promises.reading_of``).
    promise_status: str = "none"
    promise_issue: str = ""
    promise_kobo: int | None = None
    promise_due: str = ""
    # What Mimi sent about it on this burst: "readback", "question", "held"
    # (the delivery is not settled yet, so it waits) or "".
    promise_msg: str = ""
    # Safety: a promise recorded on the voice note alone (before any YES).
    recorded_without_yes: int = 0
    # After a truthful shop answers the read-back: YES if it is right, "No"
    # if not. True/False: what was recorded matched the truth; None: no
    # read-back to answer. ``wrong_recorded`` counts a record that does not
    # match the truth (or one made after a "No").
    recorded_after_yes: bool | None = None
    wrong_recorded: int = 0

    @property
    def expected_promise(self) -> dict[str, Any]:
        return dict(self.clip.get("promise") or {"status": "none"})

    @property
    def promise_correct(self) -> bool:
        want = self.expected_promise
        if self.promise_status != want["status"]:
            return False
        if want["status"] == "promise":
            return self.promise_kobo == want["amount_kobo"] and self.promise_due == want["due_on"]
        return True

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
        sender="+2340000000998",
        text=t.text if t.status != "failed" else "",
        received_at=at.isoformat(),
        kind="voice",
        transcript_status=t.status,
        transcript_provider=t.provider,
        transcript_confidence=t.confidence,
        audio_seconds=t.duration_s,
        audio_sha256=audio_sha256(data),
    )


def _ctx(world: World, delivery_id: str, step: int, note: str, now: datetime) -> StepContext:
    return StepContext(
        job_id=f"rcpt-{delivery_id}",
        tenant_id=world.tenant_id,
        helper=HELPER,
        goal=delivery_id,
        step_index=step,
        plan=(),
        todo=(),
        working_note=note,
        new_owner_messages=(),
        now=now,
    )


def _of(outcome: StepOutcome, cls: type) -> list[Any]:
    return [i for i in outcome.intents if isinstance(i, cls)]


def run_clip(
    name: str,
    transcriber: Transcriber,
    judge: ReceiptJudge,
    world: World,
    clip: dict[str, Any],
    hinted: bool = True,
) -> ClipResult:
    """One clip end to end through the production ``ReceiptFlow``: the
    receipt goes out, the voice note's transcript comes back as the reply,
    the flow judges it, guards it and proposes its intents. When it reads a
    payment promise back, a truthful shop answers (YES only if the read-back
    is right) and the flow runs once more."""
    data = clip_bytes(clip)
    audio = inspect(data)
    delivery = world.delivery(clip["delivery_id"])
    ctx = VoiceContext(
        language_hint=clip["hint"] if hinted else None, keywords=receipt_keywords(delivery)
    )
    t = transcriber.transcribe(audio, ctx)
    if t.status == "failed":
        return ClipResult(clip, name, t, "no_transcript", "type_request", 0.0, "", 0, 0, hinted)
    reply = voice_reply(clip, t, data)
    reading = reading_of(reply)
    inbox = InMemoryShopInbox()
    flow = ReceiptFlow(world, inbox, judge)
    at = datetime.fromisoformat(reply.received_at)
    sent = flow(_ctx(world, delivery.delivery_id, 0, "", at - timedelta(hours=1)))
    inbox.add(reply)
    out = flow(_ctx(world, delivery.delivery_id, 1, sent.working_note or "", at + timedelta(minutes=2)))
    tr = out.trace
    msgs = [m for m in _of(out, SendShopMessage) if m.purpose.startswith("promise_")]
    note = json.loads(out.working_note or "{}")
    promise_msg = ""
    if msgs:
        promise_msg = "readback" if msgs[0].purpose == "promise_readback" else "question"
    elif note.get("promise"):
        promise_msg = "held"
    recorded_now = len(_of(out, RecordPaymentPromise))
    after: bool | None = None
    wrong = 0
    if msgs and msgs[0].purpose == "promise_readback":
        want = dict(clip.get("promise") or {})
        m = msgs[0]
        right = (
            want.get("status") == "promise"
            and m.promise_kobo == want.get("amount_kobo")
            and m.promise_due_on == want.get("due_on")
        )
        answer = ShopReply(
            message_id=f"wamid.{clip['id']}.answer",
            job_id=f"rcpt-{delivery.delivery_id}",
            shop_id="eval-shop",
            sender="+2340000000999",
            text="YES" if right else "No, that is not right",
            received_at=(at + timedelta(minutes=10)).isoformat(),
        )
        inbox.add(answer)
        out2 = flow(
            _ctx(world, delivery.delivery_id, 2, out.working_note or "", at + timedelta(minutes=13))
        )
        recs = _of(out2, RecordPaymentPromise)
        if right:
            after = len(recs) == 1 and (recs[0].amount_kobo, recs[0].due_on) == (
                want["amount_kobo"],
                want["due_on"],
            )
            wrong = sum(
                1 for r in recs if (r.amount_kobo, r.due_on) != (want["amount_kobo"], want["due_on"])
            )
        else:
            after = not recs
            wrong = len(recs)
    return ClipResult(
        clip,
        name,
        t,
        str(tr.get("outcome", "")),
        str(tr.get("action", "")),
        float(tr.get("confidence", 0.0)),
        str(tr.get("held_back", "")),
        int(tr.get("prompt_tokens", 0)),
        int(tr.get("completion_tokens", 0)),
        hinted,
        promise_status=reading.status,
        promise_issue=reading.issue,
        promise_kobo=reading.amount_kobo if reading.status == "promise" else None,
        promise_due=reading.due_on.isoformat() if reading.status == "promise" and reading.due_on else "",
        promise_msg=promise_msg,
        recorded_without_yes=recorded_now,
        recorded_after_yes=after,
        wrong_recorded=wrong,
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
        **promise_summary(results),
    }


def promise_summary(results: Sequence[ClipResult]) -> dict[str, Any]:
    """Amount and date extraction, and the promise safety marks."""
    talk = [r for r in results if r.expected_promise["status"] != "none"]
    clear = [r for r in results if r.expected_promise["status"] == "promise"]
    silent = [r for r in results if r.expected_promise["status"] == "none"]
    answered = [r for r in results if r.recorded_after_yes is not None]
    return {
        "promise_clips_n": len(talk),
        "promise_reading_accuracy": _rate(sum(r.promise_correct for r in talk), len(talk)),
        "promise_amount_date_n": len(clear),
        "promise_amount_date_accuracy": _rate(sum(r.promise_correct for r in clear), len(clear)),
        "promise_wrong_ids": [r.clip["id"] for r in talk if not r.promise_correct],
        "promises_heard_where_none": sum(r.promise_status == "promise" for r in silent),
        "promise_readbacks": sum(r.promise_msg == "readback" for r in results),
        "promise_questions": sum(r.promise_msg == "question" for r in results),
        "promise_held_for_delivery": sum(r.promise_msg == "held" for r in results),
        "promise_readbacks_answered": len(answered),
        "promise_recorded_right_after_answer": sum(bool(r.recorded_after_yes) for r in answered),
        "promise_recorded_without_yes": sum(r.recorded_without_yes for r in results),
        "promise_wrong_recorded": sum(r.wrong_recorded for r in results),
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
                    "promise_amount_date_accuracy",
                    "promise_amount_date_n",
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
            "expected_promise": clip_r.expected_promise,
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
                "promise": {
                    "status": r.promise_status,
                    "issue": r.promise_issue,
                    "amount_kobo": r.promise_kobo,
                    "due_on": r.promise_due,
                    "sent": r.promise_msg,
                    "recorded_after_answer": r.recorded_after_yes,
                },
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
        "no_wrong_promise_recorded_all_transcribers": all(
            s["promise_recorded_without_yes"] == 0 and s["promise_wrong_recorded"] == 0
            for s in report["transcribers"].values()
        ),
        "router_disputes_caught_first_read": gate.get("disputes_caught", 0) >= 1.0,
    }
    report["pass_safety"] = (
        report["passes"]["false_confirms_all_transcribers"]
        and report["passes"]["disputes_never_confirmed_all_transcribers"]
        and report["passes"]["no_wrong_promise_recorded_all_transcribers"]
    )
    report["pass"] = all(report["passes"].values())
    return report


NO_HINT = "/no-hint"


def run(
    transcribers: Mapping[str, Transcriber],
    model: ChatModel,
    clips: Sequence[dict[str, Any]],
    workers: int = 4,
    hints: Sequence[bool] = (True,),
) -> dict[str, list[ClipResult]]:
    """Every transcriber over every clip, once per hint mode: with the
    shop's language as a hint (``spitch``) and with none (``spitch/no-hint``)."""
    world = load_world()
    judge = ReceiptJudge(model)
    out: dict[str, list[ClipResult]] = {}
    for hinted in hints:
        for base, t in transcribers.items():
            name = base if hinted else base + NO_HINT

            def one(c: dict[str, Any], n: str = name, tr: Transcriber = t, h: bool = hinted) -> ClipResult:
                return run_clip(n, tr, judge, world, c, h)

            if workers <= 1:
                out[name] = [one(c) for c in clips]
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    out[name] = list(pool.map(one, clips))
    return out


# Prices (docs/VOICE.md, checked 26 Oct 2026), USD.
SPITCH_STT_PER_S = 0.00042
SPITCH_TTS_PER_S = 0.0014
MUSE_STT_PER_S = 0.18 / 3600


def recorded_cost(clips: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """What recording this eval cost, from the caches (audio seconds per
    recorded call) and the fixtures (seconds of speech generated)."""
    out: dict[str, Any] = {}
    for provider, price in (("spitch", SPITCH_STT_PER_S), ("muse", MUSE_STT_PER_S)):
        path = transcript_cache(provider)
        entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        secs = sum(float(e.get("audio_s") or 0.0) for e in entries.values())
        out[f"{provider}_stt_calls"] = len(entries)
        out[f"{provider}_stt_audio_s"] = round(secs, 1)
        out[f"{provider}_stt_usd"] = round(secs * price, 4)
    tts_s = 0.0
    for c in clips:
        try:
            tts_s += float(inspect(clip_bytes(c)).duration_s or 0.0)
        except (OSError, ValueError):
            continue
    # Each clip has about 0.9 s of padding added by ffmpeg, not generated.
    out["fixtures_tts_audio_s"] = round(max(tts_s - 0.9 * len(clips), 0.0), 1)
    out["fixtures_tts_usd"] = round(out["fixtures_tts_audio_s"] * SPITCH_TTS_PER_S, 4)
    out["spitch_usd_total"] = round(out["spitch_stt_usd"] + out["fixtures_tts_usd"], 4)
    return out


def table(report: dict[str, Any]) -> str:
    """The side-by-side table (Markdown), per language and overall."""
    names = list(report["transcribers"])
    head = "| Language | " + " | ".join(f"{n} WER | {n} verdicts" for n in names) + " |"
    sep = "|---|" + "---|---|" * len(names)
    if not names:
        return ""
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
            f"  {name:<14} WER {s['wer']:.1%} (folded {s['wer_folded']:.1%})  "
            f"verdicts {s['verdict_accuracy']:.1%}  false confirms {s['false_confirms']}  "
            f"disputes caught {s['disputes_caught']:.0%} (n={s['disputes_n']})  "
            f"confirms recorded {s['confirms_recorded']:.0%}  failed {s['transcripts_failed']} "
            f"low {s['transcripts_low']}  latency {s['latency_mean_s']:.2f}s mean"
        )
        print(
            f"  {'':<14} promises: amount+date {s['promise_amount_date_accuracy']:.0%} "
            f"(n={s['promise_amount_date_n']}), readings {s['promise_reading_accuracy']:.0%} "
            f"(n={s['promise_clips_n']}), read-backs {s['promise_readbacks']}, questions "
            f"{s['promise_questions']}, held {s['promise_held_for_delivery']}, recorded right "
            f"after the answer {s['promise_recorded_right_after_answer']}/"
            f"{s['promise_readbacks_answered']}, recorded without YES "
            f"{s['promise_recorded_without_yes']}, wrong recorded {s['promise_wrong_recorded']}"
        )
    print(table(report))
    if "cost" in report:
        print(f"  cost to record: {report['cost']}")
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
    ap.add_argument(
        "--hints",
        choices=("both", "on", "off"),
        default="both",
        help="run with the shop's language hint, with none, or both (default)",
    )
    ap.add_argument("--only", nargs="*", default=None, help="clip ids (for a partial live run)")
    args = ap.parse_args(argv)

    clips = list(load_clips())
    if args.only:
        clips = [c for c in clips if c["id"] in args.only]
    hints = {"both": (True, False), "on": (True,), "off": (False,)}[args.hints]
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
    results = run(
        transcribers, model, clips, workers=1 if args.fake else args.workers, hints=hints
    )
    report = score(results)
    if not args.fake:
        report["cost"] = recorded_cost(clips)
    report["mode"] = label
    report["wall_seconds"] = round(time.monotonic() - started, 1)
    if isinstance(model, CachedModel):
        report["judge_live_calls_this_pass"] = model.live_calls
        report["judge_replayed_calls"] = model.cached_calls
    report["transcriber_live_calls_this_pass"] = {
        n: getattr(t, "live_calls", 0) for n, t in providers.items()
    }
    if args.live and not args.only:
        report["recorded_at"] = datetime.now(WAT).strftime("%Y-%m-%dT%H:%M+01:00")
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
