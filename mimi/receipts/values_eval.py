"""Amounts and dates from voice notes: the 50-clip eval (RAI-290).

    python -m mimi.receipts.values_eval              # replay recorded transcripts (CI)
    python -m mimi.receipts.values_eval --live       # record missing transcripts (Spitch, Muse)
    python -m mimi.receipts.values_eval --split dev --show   # per-clip detail, dev only

Each clip in ``eval/voice_values/clips.jsonl`` is a synthetic voice note (a
Spitch text-to-speech system voice plus ffmpeg market noise, no real
person's voice) in English, Pidgin, Yoruba-flavoured English, Yoruba, Igbo
or Hausa, about paying: naira in words, "20k", "two thousand five", "next
Friday", "this weekend", "ọ̀la", "gobe", "Fraịdee", instalments, "I don
pay". The labels were written before the reader was changed for them; 20
clips are ``dev`` (looked at while tuning) and 30 are ``heldout`` (never
looked at one by one while tuning, only scored).

For every clip:

1. the speech router (Spitch first for Nigerian languages, Muse Voice
   Transcribe first for English, with the shop's language as a hint)
   transcribes it, replayed from ``transcripts.<provider>.cache.json``;
2. code reads the promise (``mimi.voice.promise.read_promise``): an amount
   in integer kobo and a day in Africa/Lagos, or "unclear" with the options
   to ask about;
3. the receipt flow's promise steps run on it (``mimi.receipts.promises``):
   Mimi's read-back or question, then a shop answer that is not YES, then
   an honest shop that says YES only when the read-back is right;
4. Mimi's message is rendered as a voice reply in the shop's language
   (``mimi.voice.render``) and its figures are checked against the text.

Pass marks (from RAI-290): amount and date right on >= 95% of the clips;
zero promises recorded without the shop's YES; zero recorded with a wrong
amount or day; zero voice replies saying a figure that is not in the text.

What blocks CI (the same call Tobi made for the first voice eval on 26 Oct
2026: safety marks block, quality marks warn): every safety mark, plus the
amount-and-date accuracy must not fall below the recorded run
(``ACCURACY_FLOOR``). ``--strict`` also blocks on the 95% mark. A third pass
reads the label text itself (no speech), to show how much of a miss is the
transcriber and how much is the reader.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from mimi.receipts.inbox import ShopReply
from mimi.receipts.promises import message_for, pending_from, promise_step
from mimi.voice.audio import inspect
from mimi.voice.base import Transcriber, VoiceContext
from mimi.voice.cache import CachedTranscriber
from mimi.voice.consensus import SECOND_OPINION
from mimi.voice.promise import PromiseReading, read_promise
from mimi.voice.render import render_voice
from mimi.voice.router import DEFAULT_POLICY, VoiceRouter
from mimi.voice.vocab import VOCAB_PROVIDERS

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "eval" / "voice_values"
MANIFEST = DIR / "clips.jsonl"
REPORT = DIR / "values.report.json"
PROVIDERS = ("spitch", "muse")
PASS_ACCURACY = 0.95
# Tobi's bar for the transcriber pass (28 Oct 2026): >= 90% on the fresh
# held-out split (``fresh``, 60 clips, labels committed before tuning).
PASS_FRESH = 0.90
SPLITS = ("dev", "heldout", "fresh")
# Accuracy through the speech router on every clip that has audio and a
# recorded transcript. 28 Oct 2026: 37/50 (74%) with one transcriber; 42/50
# (84%) with the money words and the second opinion (``mimi.voice.consensus``).
# A change that reads fewer clips right fails CI; raise this when it improves.
ACCURACY_FLOOR = 0.84
# USD per second of audio, from the providers' price pages as recorded in
# eval/voice/voice.report.json (26 Oct 2026).
SPITCH_STT_USD_S = 0.1732 / 412.4
MUSE_STT_USD_S = 0.0206 / 412.4
SPITCH_TTS_USD_S = 0.2257 / 161.2
# Voice reply language for each clip language (the shop's own choice in
# production; here the language the clip is in).
VOICE_LANG = {
    "english": "english", "pidgin": "pidgin", "yoruba_english": "yoruba",
    "yoruba": "yoruba", "igbo": "igbo", "hausa": "hausa",
}


def load_clips() -> list[dict[str, Any]]:
    return [json.loads(x) for x in MANIFEST.read_text(encoding="utf-8").splitlines() if x.strip()]


def cache_path(provider: str) -> Path:
    return DIR / f"transcripts.{provider}.cache.json"


def router(live: bool) -> tuple[VoiceRouter, dict[str, CachedTranscriber]]:
    if live:
        from mimi.voice.env import live_transcribers

        inner = live_transcribers()
        missing = [p for p in PROVIDERS if p not in inner]
        if missing:
            raise SystemExit(f"--live needs keys for {missing}")
        cached = {p: CachedTranscriber(inner[p], cache_path(p), mode="through") for p in PROVIDERS}
    else:
        cached = {
            p: CachedTranscriber(None, cache_path(p), mode="replay", name=p) for p in PROVIDERS
        }
    providers: dict[str, Transcriber] = dict(cached)
    return VoiceRouter(providers, DEFAULT_POLICY, second=SECOND_OPINION,
                       vocab_providers=VOCAB_PROVIDERS), cached


def has_audio(clip: dict[str, Any]) -> bool:
    return (DIR / clip["file"]).exists()


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def values_right(reading: PromiseReading, expect: dict[str, Any]) -> tuple[bool, str]:
    """Did code read what the shop said? For an unclear label: nothing
    wrong asserted, and the options the shop must choose between offered."""
    want = expect["status"]
    if want == "promise":
        if reading.status != "promise":
            return False, f"{reading.status}:{reading.issue}"
        ok = reading.amount_kobo == expect["amount_kobo"] and reading.due_on == _d(expect["due_on"])
        return ok, "" if ok else f"read {reading.amount_kobo} {reading.due_on}"
    if want == "unclear":
        if reading.status != "unclear":
            return False, f"{reading.status} {reading.amount_kobo} {reading.due_on}"
        if reading.issue == "low_quality_transcript":
            return False, "low transcript"
        if reading.amount_kobo is not None and reading.amount_kobo != expect.get("amount_kobo"):
            return False, f"wrong amount {reading.amount_kobo}"
        if reading.due_on is not None and reading.due_on != _d(expect.get("due_on")):
            return False, f"wrong day {reading.due_on}"
        if expect.get("amount_kobo") is not None and reading.amount_kobo is None:
            return False, "lost the amount"
        if expect.get("due_on") and reading.due_on is None:
            return False, "lost the day"
        want_ao = set(expect.get("amount_options") or [])
        want_do = {_d(x) for x in expect.get("date_options") or []}
        if want_ao and set(reading.amount_options) != want_ao:
            return False, f"amount options {list(reading.amount_options)}"
        if want_do and set(reading.date_options) != want_do:
            return False, f"date options {list(reading.date_options)}"
        return True, ""
    ok = reading.status == want
    return ok, "" if ok else f"{reading.status} {reading.amount_kobo} {reading.due_on}"


@dataclass
class ClipResult:
    id: str
    split: str
    lang: str
    phenomenon: str
    transcript: str
    status: str
    provider: str
    reading: dict[str, Any]
    right: bool
    why: str
    message_purpose: str
    message: str
    recorded_without_yes: bool
    recorded_wrong: bool
    recorded_after_yes: bool
    voice_script: str
    voice_language: str
    voice_why_not: str
    voice_figures_ok: bool


def _reading_json(r: PromiseReading) -> dict[str, Any]:
    return {
        "status": r.status, "issue": r.issue, "amount_kobo": r.amount_kobo,
        "due_on": r.due_on.isoformat() if r.due_on else None,
        "amount_options": list(r.amount_options),
        "date_options": [d.isoformat() for d in r.date_options],
    }


@dataclass(frozen=True)
class _Said:
    text: str
    status: str = "ok"
    provider: str = "reference"


def run_clip(clip: dict[str, Any], tr: Transcriber | None, *, hint: bool = True) -> ClipResult:
    """One clip through speech, the reader, the promise steps and the voice
    reply. ``tr=None`` reads the label text itself (no speech)."""
    t: Any
    if tr is None:
        t = _Said(clip["reference"])
    else:
        audio = inspect((DIR / clip["file"]).read_bytes())
        t = tr.transcribe(audio, VoiceContext(language_hint=clip["hint"] if hint else None,
                                              said_at=clip["said_at"]))
    said_at = clip["said_at"]
    reply = ShopReply(
        message_id=f"wamid.{clip['id']}", job_id="job-eval", shop_id="shop-eval",
        sender="+2340000000000", text=t.text, received_at=said_at, kind="voice",
        transcript_status=t.status, transcript_provider=t.provider,
    )
    if t.status == "failed":
        reading = PromiseReading("none")
    else:
        reading = read_promise(t.text, said_at, transcript_ok=t.status == "ok")
    right, why = values_right(reading, clip["expect"])
    exp = clip["expect"]
    purpose = message = ""
    rec_no_yes = rec_wrong = rec_yes = False
    script = vlang = vwhy = ""
    vok = True
    if reading.found:
        pending = pending_from(reading, reply)
        purpose, message = message_for(pending, "dlv-eval-01")
        pending = {**pending, "asked": "readback" if purpose == "promise_readback" else "question",
                   "sent": 1}
        later = datetime.fromisoformat(said_at).replace(minute=30).isoformat()
        # A shop answer that is not YES never records anything.
        for words in ("Thank you", "ok", "No"):
            nope = ShopReply(f"wamid.{clip['id']}.n", "job-eval", "shop-eval", "+2340000000000",
                             words, later)
            if promise_step(pending, [nope]).action == "record":
                rec_no_yes = True
        # An honest shop: YES only when the read-back is right.
        truth_ok = (
            exp["status"] == "promise"
            and pending["status"] == "promise"
            and pending["amount_kobo"] == exp["amount_kobo"]
            and pending["due_on"] == exp["due_on"]
        )
        answer = "YES" if truth_ok else "No, that is not right"
        step = promise_step(pending, [ShopReply(f"wamid.{clip['id']}.y", "job-eval", "shop-eval",
                                                "+2340000000000", answer, later)])
        if step.action == "record":
            if answer != "YES":
                rec_no_yes = True
            p = step.pending
            if not (p["amount_kobo"] == exp.get("amount_kobo") and p["due_on"] == exp.get("due_on")):
                rec_wrong = True
            rec_yes = answer == "YES"
        today = datetime.fromisoformat(said_at).date()
        vs, vwhy = render_voice(
            message, VOICE_LANG[clip["lang"]], today=today,
            expect_kobo=(pending["amount_kobo"],) if purpose == "promise_readback" else None,
            expect_days=(_d(pending["due_on"]),) if purpose == "promise_readback" else None,
        )
        if vs is not None:
            script, vlang, vok = vs.script, vs.voice, vs.checked
    return ClipResult(
        clip["id"], clip["split"], clip["lang"], clip["phenomenon"], t.text, t.status,
        t.provider, _reading_json(reading), right, why, purpose, message, rec_no_yes, rec_wrong,
        rec_yes, script, vlang, vwhy, vok,
    )


def summarize(results: list[ClipResult]) -> dict[str, Any]:
    def acc(rs: list[ClipResult]) -> dict[str, Any]:
        n = len(rs)
        k = sum(r.right for r in rs)
        return {"right": k, "n": n, "accuracy": round(k / n, 4) if n else None}

    by_split = defaultdict(list)
    by_lang = defaultdict(list)
    for r in results:
        by_split[r.split].append(r)
        by_lang[r.lang].append(r)
    promises = [r for r in results if r.reading["status"] == "promise"]
    voiced = [r for r in results if r.voice_script]
    return {
        "all": acc(results),
        "by_split": {k: acc(v) for k, v in sorted(by_split.items())},
        "by_language": {k: acc(v) for k, v in sorted(by_lang.items())},
        "read_back_wrong": sum(
            1 for r in promises if not r.right and r.message_purpose == "promise_readback"),
        "recorded_without_yes": sum(r.recorded_without_yes for r in results),
        "recorded_wrong": sum(r.recorded_wrong for r in results),
        "recorded_after_yes": sum(r.recorded_after_yes for r in results),
        "low_or_failed_transcripts": sum(1 for r in results if r.status != "ok"),
        "voice_replies": len(voiced),
        "voice_replies_text_only": sum(1 for r in results if r.message and not r.voice_script),
        "voice_figures_not_in_text": sum(1 for r in voiced if not r.voice_figures_ok),
        "statuses": dict(Counter(r.reading["status"] for r in results)),
    }


def passes(s: dict[str, Any]) -> dict[str, bool]:
    return {
        "accuracy >= 95%": (s["all"]["accuracy"] or 0) >= PASS_ACCURACY,
        "0 recorded without YES": s["recorded_without_yes"] == 0,
        "0 recorded wrong": s["recorded_wrong"] == 0,
        "0 voice figures not in text": s["voice_figures_not_in_text"] == 0,
    }


HEARD_DIR = DIR / "replies"
HEARD_VOICES = ("english", "pidgin", "yoruba:en", "igbo:en", "hausa:en")


def heard_back(results: list[ClipResult], tr: Transcriber, *, live: bool) -> dict[str, Any]:
    """Speak one read-back per voice (Spitch, recorded in ``replies/``), hear
    it back with the speech router and check code reads the same amount and
    day. A report line, not a gate: a misheard voice note is caught by the
    text that always goes with it and by the shop's own YES."""
    from mimi.voice.render import figures
    from mimi.voice.speak import CachedSpeaker, SpeechCacheMiss

    inner = None
    if live:
        from mimi.voice.env import speaker_from_env

        inner = speaker_from_env()
    speaker = CachedSpeaker(inner, HEARD_DIR, mode="through" if live else "replay",
                            name="spitch")
    from mimi.voice.speak import VOICES, speech_key

    def recorded(r: ClipResult) -> bool:
        key = speech_key("spitch", VOICES[r.voice_language][1], r.voice_language, r.voice_script)
        return key in speaker._index

    # One read-back per voice; an already recorded one first, so a replay
    # does not depend on which clip happens to come first.
    picked: dict[str, ClipResult] = {}
    for r in sorted(results, key=lambda r: not recorded(r) if r.voice_script else True):
        if r.message_purpose == "promise_readback" and r.voice_script and \
                r.voice_language in HEARD_VOICES and r.voice_language not in picked:
            picked[r.voice_language] = r
    rows: list[dict[str, Any]] = []
    for voice, r in picked.items():
        try:
            audio = speaker.speak(r.voice_script, voice)
        except SpeechCacheMiss:
            rows.append({"clip": r.id, "voice": voice, "heard": "", "right": None})
            continue
        clip = inspect(audio.data)
        t = tr.transcribe(clip, VoiceContext(language_hint="en"))
        said = next(c for c in load_clips() if c["id"] == r.id)["said_at"]
        d0 = datetime.fromisoformat(said).date()
        want_k, want_d, _ = figures(r.voice_script, d0)
        got_k, got_d, _ = figures(t.text, d0)
        rows.append({"clip": r.id, "voice": voice, "seconds": audio.duration_s,
                     "heard": t.text, "right": got_k == want_k and set(got_d) == set(want_d)})
    tts_s = sum(float(str(v.get("seconds") or 0)) for v in speaker._index.values())
    return {"replies": rows, "right": sum(1 for x in rows if x["right"]),
            "n": len(rows), "tts_audio_s": round(tts_s, 1),
            "tts_usd": round(tts_s * SPITCH_TTS_USD_S, 4)}


def _cost(cached: dict[str, CachedTranscriber]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p, c in cached.items():
        secs = sum(float(e.get("audio_s") or 0) for e in c._entries.values())
        out[f"{p}_stt_recorded_calls"] = len(c)
        out[f"{p}_stt_audio_s"] = round(secs, 1)
        out[f"{p}_stt_usd"] = round(secs * (SPITCH_STT_USD_S if p == "spitch" else MUSE_STT_USD_S), 4)
    fx = DIR / "fixtures.cost.json"
    if fx.exists():
        f = json.loads(fx.read_text())
        out["fixtures_tts_audio_s"] = f["tts_audio_s"]
        out["fixtures_tts_usd"] = round(f["tts_audio_s"] * SPITCH_TTS_USD_S, 4)
    out["usd_total"] = round(sum(v for k, v in out.items() if k.endswith("_usd")), 4)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--live", action="store_true", help="record missing transcripts")
    ap.add_argument("--split", choices=("all", *SPLITS), default="all")
    ap.add_argument("--show", action="store_true", help="per-clip detail (dev only)")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--strict", action="store_true", help="also block on the 95%% mark")
    args = ap.parse_args(argv)
    if args.show and args.split != "dev":
        print("--show is for the dev split only: held-out clips are scored, not read", file=sys.stderr)
        return 2
    tr, cached = router(args.live)
    wanted = [c for c in load_clips() if args.split in ("all", c["split"])]
    clips = [c for c in wanted if has_audio(c)]
    no_audio = Counter(c["split"] for c in wanted if not has_audio(c))
    if no_audio:
        print(f"clips with labels but no audio yet (make them with `make voice-values-fixtures`,"
              f" needs Spitch credit): {dict(no_audio)}")
    passes_out: dict[str, Any] = {}
    runs: tuple[tuple[str, Transcriber | None, bool], ...] = (
        ("router", tr, True), ("router_no_hint", tr, False), ("reference_text", None, True))
    for name, which, hint in runs:
        results = [run_clip(c, which, hint=hint) for c in clips]
        s = summarize(results)
        passes_out[name] = {"summary": s, "clips": [r.__dict__ for r in results]}
        if args.show:
            for r in results:
                mark = "ok " if r.right else "MISS"
                print(f"{mark} {r.id} [{r.lang}] {r.phenomenon}: {r.transcript!r} -> "
                      f"{r.reading} {r.why}")
                if r.voice_script or r.voice_why_not:
                    print(f"     voice[{r.voice_language}]: {r.voice_script or r.voice_why_not}")
        print(f"{name}: right {s['all']['right']}/{s['all']['n']} "
              f"({s['all']['accuracy']:.0%}), by split {s['by_split']}")
        langs = {k: f"{v['right']}/{v['n']}" for k, v in s["by_language"].items()}
        print(f"  by language: {langs}")
        print(f"  read-backs wrong {s['read_back_wrong']}, recorded without YES "
              f"{s['recorded_without_yes']}, recorded wrong {s['recorded_wrong']}, recorded after "
              f"YES {s['recorded_after_yes']}, low/failed transcripts "
              f"{s['low_or_failed_transcripts']}")
        print(f"  voice replies {s['voice_replies']} (text only {s['voice_replies_text_only']}), "
              f"voice figures not in text {s['voice_figures_not_in_text']}")
    heard: dict[str, Any] = {}
    if args.split == "all":
        # Heard back with the plain router (no money words, no second
        # opinion): it checks the voice reply, not the shop's note.
        plain = VoiceRouter(dict(cached), DEFAULT_POLICY)
        heard = heard_back([ClipResult(**c) for c in passes_out["router"]["clips"]], plain,
                           live=args.live)
        rec = [x for x in heard["replies"] if x["right"] is not None]
        print(f"voice replies heard back: amounts and days right {heard['right']}/{len(rec)} "
              f"recorded ({heard['n'] - len(rec)} voices not recorded for these scripts yet)")
        for x in rec:
            if not x["right"]:
                print(f"  {x['clip']} [{x['voice']}] heard: {x['heard']!r}")
    main_s = passes_out["router"]["summary"]
    marks = passes(main_s)
    fresh = main_s["by_split"].get("fresh")
    marks["fresh held-out >= 90%"] = bool(fresh and (fresh["accuracy"] or 0) >= PASS_FRESH)
    safety = all(
        v
        for run in passes_out.values()
        for k, v in passes(run["summary"]).items()
        if not k.startswith("accuracy")
    )
    floor_ok = (main_s["all"]["accuracy"] or 0) >= ACCURACY_FLOOR if args.split == "all" else True
    ok = all(marks.values()) and safety
    blocking = safety and floor_ok and (ok or not args.strict)
    cost = _cost(cached)
    if heard:
        cost["voice_replies_tts_audio_s"] = heard["tts_audio_s"]
        cost["voice_replies_tts_usd"] = heard["tts_usd"]
        cost["usd_total"] = round(cost["usd_total"] + heard["tts_usd"], 4)
    print("pass marks:", marks, "| safety, every pass:", safety,
          f"| accuracy floor {ACCURACY_FLOOR:.0%}:", floor_ok)
    if not ok and blocking:
        print("SAFETY PASS; quality mark not met (a warning; --strict blocks on it)")
    print("cost (recorded):", cost)
    if args.split == "all" and not args.no_write:
        recorded_at = None
        if REPORT.exists():
            recorded_at = json.loads(REPORT.read_text()).get("recorded_at")
        if args.live or recorded_at is None:
            recorded_at = datetime.now().astimezone().isoformat(timespec="seconds")
        REPORT.write_text(json.dumps({
            "pass_marks": marks, "safety_every_pass": safety, "accuracy_floor": ACCURACY_FLOOR,
            "pass": ok, "clips_without_audio": dict(no_audio),
            "cost": cost, "recorded_at": recorded_at, "voice_replies_heard_back": heard,
            "passes": passes_out,
        }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0 if blocking else 1


if __name__ == "__main__":
    sys.exit(main())
