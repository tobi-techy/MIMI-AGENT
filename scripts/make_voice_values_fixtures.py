"""Make the synthetic clips for the amounts-and-dates eval (``eval/voice_values``).

    SPITCH_API_KEY=... python scripts/make_voice_values_fixtures.py          # missing clips only
    SPITCH_API_KEY=... python scripts/make_voice_values_fixtures.py --only vv-03

Same recipe as ``scripts/make_voice_fixtures.py``: the text in
``eval/voice_values/clips.jsonl`` is spoken by a Spitch text-to-speech
system voice (no real person's voice), ffmpeg adds synthetic market noise
and encodes a WhatsApp-style mono Ogg/Opus voice note. The seconds of speech
generated are added to ``eval/voice_values/fixtures.cost.json`` so the
report can show what the fixtures cost.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_voice_fixtures import mix  # noqa: E402

from mimi.voice.audio import _fix_wav_sizes, wav_duration  # noqa: E402
from mimi.voice.tts import SpitchSpeech  # noqa: E402

DIR = ROOT / "eval" / "voice_values"
MANIFEST = DIR / "clips.jsonl"
COST = DIR / "fixtures.cost.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", nargs="*", default=None)
    args = ap.parse_args(argv)
    key = os.environ.get("SPITCH_API_KEY", "").strip()
    if not key:
        print("SPITCH_API_KEY is not set", file=sys.stderr)
        return 2
    tts = SpitchSpeech(api_key=key)
    cost = json.loads(COST.read_text()) if COST.exists() else {"tts_calls": 0, "tts_audio_s": 0.0}
    rows = [json.loads(x) for x in MANIFEST.read_text(encoding="utf-8").splitlines() if x]
    for i, row in enumerate(rows):
        out = DIR / row["file"]
        if args.only and row["id"] not in args.only:
            continue
        if out.exists() and not args.force:
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        wav = _fix_wav_sizes(
            tts.generate(row["reference"], voice=row["tts"]["voice"], language=row["tts"]["language"])
        )
        seconds = wav_duration(wav)
        cost["tts_calls"] += 1
        cost["tts_audio_s"] = round(cost["tts_audio_s"] + seconds, 2)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "speech.wav"
            src.write_bytes(wav)
            mix(src, out, row["noise"], "ogg", seed=2000 + i)
        print(f"{row['id']}: {seconds:.1f}s speech, {out.stat().st_size} bytes")
    COST.write_text(json.dumps(cost, indent=1) + "\n")
    print(f"speech calls this run: {tts.calls}; total {cost}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
