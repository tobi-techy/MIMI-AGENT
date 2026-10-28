"""Audition Spitch voices for Mimi's voice replies (RAI-290), and hear them back.

    SPITCH_API_KEY=... META_API_KEY=... python scripts/voice_audition.py --max-usd 0.40
    python scripts/voice_audition.py --replay        # offline, from the recorded audio

For every voice language Mimi speaks (``mimi.voice.speak.VOICES``) the same
two replies (a read-back and a question, rendered and paced by
``mimi.voice.render``) are spoken by each candidate Spitch system voice. Each
take is then:

* heard back by Muse Voice Transcribe, and code checks the amounts and days
  it hears are exactly the script's (a take that fails this can never be
  the pick);
* measured: words per second (a calm, clear pace is about 2.3 to 3.0) and
  pauses longer than 0.25 s (``ffmpeg silencedetect``; a sentence break
  should give one, a figure should not be split by one);
* given a naturalness proxy from 1 to 5: 5 when figures are exact, the pace
  is in range, the heard-back word error rate is under 10% and the pauses
  fall at the sentence breaks; one point off for each miss.

The proxy is not a person listening. The report lists the takes to play, so
Tobi (or a native speaker) gives the final ear; the pick per language goes
into ``VOICES`` only after that. Audio is recorded under
``eval/voice_values/audition/`` (synthetic voices only) so replays cost
nothing. Spitch speech is $0.0014 a second; ``--max-usd`` stops before the
estimate goes over.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mimi.voice.audio import inspect  # noqa: E402
from mimi.voice.base import VoiceContext  # noqa: E402
from mimi.voice.render import figures, render_voice  # noqa: E402
from mimi.voice.wer import wer  # noqa: E402

OUT = ROOT / "eval" / "voice_values" / "audition"
TODAY = date(2026, 10, 28)
TEXTS = (
    "You said you will pay N48,500 on Thursday 29 October 2026 for delivery dlv-aud-01. "
    "Reply YES if that is right, or tell me the right amount and day. "
    "I only note your promise; I do not take or move money.",
    "Did you mean N2,500 or N2,005 for delivery dlv-aud-01 on Friday 30 October 2026? "
    "Please write the full amount in naira.",
)
# Spitch system voices by the language they were designed around
# (https://docs.spitch.app/concepts/voices, 28 Oct 2026).
CANDIDATES: dict[str, tuple[str, ...]] = {
    "english": ("lucy", "lina", "kani", "remi", "john", "jude", "henry", "kingsley"),
    "pidgin": ("tega", "boma", "ufoma", "justice"),
    "yoruba:en": ("funmi", "sade", "femi", "segun"),
    "igbo:en": ("ngozi", "amara", "obinna", "ebuka"),
    "hausa:en": ("amina", "zainab", "aliyu", "hasan"),
}
TTS_USD_S = 0.0014


def pauses(wav: bytes) -> list[float]:
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        f.write(wav)
        f.flush()
        err = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", f.name, "-af", "silencedetect=n=-35dB:d=0.25",
             "-f", "null", "-"], capture_output=True, text=True, check=False).stderr
    return [float(x) for x in re.findall(r"silence_duration: ([\d.]+)", err)]


def score(exact: bool, wps: float, w: float, n_pauses: int, sentences: int) -> int:
    s = 5
    s -= 0 if exact else 2
    s -= 0 if 2.3 <= wps <= 3.0 else 1
    s -= 0 if w < 0.10 else 1
    s -= 0 if sentences - 1 <= n_pauses <= sentences + 2 else 1
    return max(s, 1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--max-usd", type=float, default=0.40)
    args = ap.parse_args(argv)
    from mimi.voice import speak as sp
    from mimi.voice.audio import to_wav16k
    from mimi.voice.env import speaker_from_env, transcriber_from_env

    scripts = []
    for text in TEXTS:
        vs, why = render_voice(text, "english", today=TODAY)
        assert vs is not None, why
        scripts.append(vs.script)
    est = sum(len(s.split()) / 2.7 for s in scripts) * sum(len(v) for v in CANDIDATES.values())
    print(f"{sum(len(v) for v in CANDIDATES.values())} voices x {len(scripts)} scripts, about "
          f"{est:.0f} s of speech, ${est * TTS_USD_S:.2f} if nothing is recorded yet")
    inner = None if args.replay else speaker_from_env()
    hear = transcriber_from_env({} if args.replay else None)
    rows = []
    spent = 0.0
    for lang, voices in CANDIDATES.items():
        for voice in voices:
            sp.VOICES[f"aud:{voice}"] = (sp.VOICES[lang][0], voice)
            cached = sp.CachedSpeaker(inner, OUT, mode="replay" if args.replay else "through",
                                      name="spitch")
            for script in scripts:
                try:
                    audio = cached.speak(script, f"aud:{voice}")
                except Exception as exc:  # noqa: BLE001 - report and move on
                    rows.append({"language": lang, "voice": voice, "script": script[:40],
                                 "error": str(exc)})
                    continue
                if cached.live_calls:
                    spent += audio.duration_s * TTS_USD_S
                clip = inspect(audio.data)
                heard = hear.transcribe(clip, VoiceContext(language_hint="en")).text
                exact = figures(heard, TODAY)[:2] == figures(script, TODAY)[:2]
                w = wer(script, heard)
                wps = len(script.split()) / max(audio.duration_s, 0.1)
                gaps = pauses(to_wav16k(clip).data)
                sentences = len(re.findall(r"[.?!]", script))
                rows.append({"language": lang, "voice": voice, "seconds": audio.duration_s,
                             "words_per_s": round(wps, 2), "pauses": len(gaps), "wer": round(w, 3),
                             "figures_exact": exact, "naturalness_proxy": score(
                                 exact, wps, w, len(gaps), sentences),
                             "heard": heard})
                if spent > args.max_usd:
                    print(f"stopped: spent about ${spent:.2f}, over --max-usd")
                    break
    picks = {}
    for lang in CANDIDATES:
        best = None
        for voice in CANDIDATES[lang]:
            takes = [r for r in rows if r["language"] == lang and r["voice"] == voice and "error" not in r]
            if len(takes) < len(scripts) or not all(r["figures_exact"] for r in takes):
                continue
            s = sum(r["naturalness_proxy"] for r in takes) / len(takes)
            if best is None or s > best[1]:
                best = (voice, s)
        picks[lang] = best
    report = {"scripts": scripts, "takes": rows, "proxy_pick": picks,
              "note": "proxy only; a person listens before VOICES changes"}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "audition.report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps(picks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
