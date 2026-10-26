"""Voice notes: audio checks, adapters (mocked HTTP), router, cache, fake, WER."""

from __future__ import annotations

import io
import json
import wave
from pathlib import Path

import httpx
import pytest

from mimi.voice import (
    AudioClip,
    CachedTranscriber,
    FakeTranscriber,
    MuseTranscriber,
    SpitchTranscriber,
    Transcript,
    TranscriptCacheMiss,
    VoiceContext,
    VoiceRouter,
    read_voice_note,
    transcriber_from_env,
)
from mimi.voice import audio as va
from mimi.voice.base import AudioError
from mimi.voice.fixtures import clip_bytes, load_clips
from mimi.voice.quality import assess
from mimi.voice.router import DEFAULT_POLICY
from mimi.voice.wer import corpus_wer, normalise, wer

CLIPS = load_clips()
OGG = next(c for c in CLIPS if c["file"].endswith(".ogg"))
needs_ffmpeg = pytest.mark.skipif(not va.have_ffmpeg(), reason="ffmpeg not installed")
KEY = "test-key"  # pragma: allowlist secret (fake test value)


def wav_bytes(seconds: float = 1.0, rate: int = 16000, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate) * channels)
    return buf.getvalue()


# ---------------------------------------------------------------- fixtures


def test_fixture_manifest_is_synthetic_and_covers_languages() -> None:
    assert 12 <= len(CLIPS) <= 16
    assert all(c["synthetic"] and c["origin"].startswith("synthetic-generated") for c in CLIPS)
    langs = {c["lang"] for c in CLIPS}
    assert {"english", "pidgin", "yoruba", "igbo", "hausa"} <= langs
    cats = {c["category"] for c in CLIPS}
    assert cats == {"confirm", "dispute", "partial", "unclear", "unrelated"}
    assert any("na 4 carton, no be 6" in c["reference"].lower() for c in CLIPS)
    assert {c["noise"] for c in CLIPS} == {"none", "light", "heavy"}
    for c in CLIPS:
        p = Path("eval/voice") / c["file"]
        assert p.exists() and p.stat().st_size < 40_000, c["id"]


def test_ogg_duration_without_ffmpeg() -> None:
    clip = va.inspect(clip_bytes(OGG))
    assert clip.format == "ogg"
    assert clip.duration_s is not None and 1.0 < clip.duration_s < 10.0


@needs_ffmpeg
def test_mp3_and_m4a_fixtures_are_read() -> None:
    for c in CLIPS:
        if c["file"].endswith((".mp3", ".m4a")):
            clip = va.inspect(clip_bytes(c))
            assert clip.format in ("mp3", "m4a") and clip.duration_s


# ---------------------------------------------------------------- audio limits


def test_limits_and_formats() -> None:
    assert va.inspect(wav_bytes(1.0)).duration_s == pytest.approx(1.0)
    with pytest.raises(AudioError, match="empty"):
        va.inspect(b"")
    with pytest.raises(AudioError, match="unsupported"):
        va.inspect(b"<html>not audio</html>" * 10)
    with pytest.raises(AudioError, match="too short"):
        va.inspect(wav_bytes(0.1))
    with pytest.raises(AudioError, match="over the"):
        va.inspect(wav_bytes(200.0, rate=8000))
    with pytest.raises(AudioError, match="bytes"):
        va.inspect(b"RIFF" + b"\x00" * (va.MAX_BYTES + 1))


def test_read_voice_note_never_raises_on_bad_audio() -> None:
    t = read_voice_note(b"garbage bytes", FakeTranscriber({}), VoiceContext())
    assert t.status == "failed" and "unsupported" in t.reason
    t = read_voice_note(wav_bytes(1.0), None, VoiceContext())
    assert t.status == "failed" and "no transcriber" in t.reason


@needs_ffmpeg
def test_conversion_to_wav16k() -> None:
    clip = va.inspect(clip_bytes(OGG))
    wav = va.to_wav16k(clip)
    assert wav.format == "wav" and va.wav_params(wav.data) == (1, 2, 16000)
    assert va.wav_duration(wav.data) == pytest.approx(clip.duration_s, abs=0.1)
    same = AudioClip(wav_bytes(1.0), "wav", 1.0)
    assert va.to_wav16k(same) is same


# ---------------------------------------------------------------- quality


def _t(text: str, seconds: float = 3.0, conf: float | None = None) -> Transcript:
    return Transcript(text, "ok", "x", confidence=conf, duration_s=seconds)


def test_quality_checks() -> None:
    assert assess(_t("yes everything complete")).status == "ok"
    assert assess(_t("   ")).status == "failed"
    assert assess(_t("northern rock " * 20, 4.0)).status == "low"
    assert assess(_t("yes", 20.0)).status == "low"
    assert assess(_t("?? !! ...")).status == "low"
    assert assess(_t("yes all complete", conf=0.3)).status == "low"
    assert assess(_t("yes all complete", conf=0.9)).status == "ok"


# ---------------------------------------------------------------- spitch


def test_spitch_request_shape_and_parse() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200,
            json={
                "request_id": "r1",
                "text": "the noodles no complete o",
                "segments": None,
                "detected_language": "en",
            },
        )

    s = SpitchTranscriber(api_key=KEY, transport=httpx.MockTransport(handler))
    clip = va.inspect(clip_bytes(OGG))
    t = s.transcribe(clip, VoiceContext("pcm", ("Sunrise", "Noodles")))
    assert (t.status, t.text, t.provider, t.language) == (
        "ok",
        "the noodles no complete o",
        "spitch",
        "en",
    )
    assert t.confidence is None
    req = seen[0]
    assert str(req.url) == "https://api.spitch.app/v1/transcriptions"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["x-data-retention"] == "false"
    body = req.content
    assert b'name="language"\r\n\r\nen' in body  # Pidgin goes as en
    assert b'name="special_words"\r\n\r\nSunrise,Noodles' in body
    assert b'name="content"; filename="voice.ogg"' in body
    assert s.form(VoiceContext("yo"))["language"] == "yo"
    assert "language" not in s.form(VoiceContext(None))


def test_spitch_retries_once_then_fails_safe() -> None:
    calls = {"n": 0}

    def flaky(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(200, json={"request_id": "r", "text": "yes"})

    s = SpitchTranscriber(api_key=KEY, transport=httpx.MockTransport(flaky), sleep=lambda _: None)
    clip = AudioClip(wav_bytes(1.0), "wav", 1.0)
    assert s.transcribe(clip, VoiceContext()).text == "yes" and calls["n"] == 2

    def timeout(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    s2 = SpitchTranscriber(
        api_key=KEY, transport=httpx.MockTransport(timeout), sleep=lambda _: None
    )
    t = s2.transcribe(clip, VoiceContext())
    assert t.status == "failed" and "ReadTimeout" in t.reason and not t.answered

    bad = {"n": 0}

    def reject(req: httpx.Request) -> httpx.Response:
        bad["n"] += 1
        return httpx.Response(400, json={"detail": "Invalid language"})

    s3 = SpitchTranscriber(api_key=KEY, transport=httpx.MockTransport(reject))
    t3 = s3.transcribe(clip, VoiceContext())
    assert t3.status == "failed" and "400" in t3.reason and bad["n"] == 1
    assert KEY not in t3.reason


# ---------------------------------------------------------------- muse


@needs_ffmpeg
def test_muse_request_shape_converts_ogg_to_wav() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200,
            json={
                "sessionId": "s",
                "transcript": "Yes, sir. Everything is complete.",
                "audioDurationMs": 3000,
                "turns": [],
            },
        )

    m = MuseTranscriber(api_key=KEY, transport=httpx.MockTransport(handler))
    t = m.transcribe(va.inspect(clip_bytes(OGG)), VoiceContext("en", ("Sunrise",)))
    assert t.status == "ok" and t.text.startswith("Yes, sir") and t.provider == "muse"
    req = seen[0]
    assert str(req.url) == "https://api.meta.ai/v1/asr/transcribe"
    assert req.headers["accept"] == "application/json"
    body = req.content
    start = body.index(b'name="request"')
    part = body[start:].split(b"\r\n\r\n", 1)[1].split(b"\r\n--", 1)[0]
    request = json.loads(part)
    assert request == {
        "model": "muse-voice-transcribe-1.0",
        "audioEncoding": "WAV",
        "mode": "PUSH_TO_TALK",
        "keywords": ["Sunrise"],
        "languageBias": ["English"],
    }
    audio = body[body.index(b'name="audio"') :]
    assert b"RIFF" in audio and b"WAVE" in audio
    assert "languageBias" not in m.request_part(VoiceContext("yo"))


def test_muse_bad_shape_is_failed() -> None:
    m = MuseTranscriber(
        api_key=KEY, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    t = m.transcribe(AudioClip(wav_bytes(1.0), "wav", 1.0), VoiceContext())
    assert t.status == "failed" and "shape" in t.reason


# ---------------------------------------------------------------- router


class Scripted:
    def __init__(self, name: str, status: str, text: str = "x y z") -> None:
        self.name, self.status, self.text, self.calls = name, status, text, 0

    def transcribe(self, clip: AudioClip, ctx: VoiceContext) -> Transcript:
        self.calls += 1
        return Transcript(self.text, self.status, self.name, reason=f"{self.name} {self.status}")  # type: ignore[arg-type]


CLIP = AudioClip(wav_bytes(1.0), "wav", 1.0)


def test_router_policy_and_fallback() -> None:
    sp, mu = Scripted("spitch", "ok", "spitch text"), Scripted("muse", "ok", "muse text")
    r = VoiceRouter({"spitch": sp, "muse": mu})
    assert r.transcribe(CLIP, VoiceContext("yo")).text == "spitch text"
    assert r.transcribe(CLIP, VoiceContext(None)).text == "spitch text"
    assert r.transcribe(CLIP, VoiceContext("en")).text == "muse text"  # by the eval numbers
    assert DEFAULT_POLICY["ha"][0] == DEFAULT_POLICY["ig"][0] == "spitch"

    low = VoiceRouter({"spitch": Scripted("spitch", "low"), "muse": mu})
    t = low.transcribe(CLIP, VoiceContext("ig"))
    assert t.provider == "muse" and t.tried == ("spitch:low", "muse:ok")

    both_low = VoiceRouter({"spitch": Scripted("spitch", "low"), "muse": Scripted("muse", "low")})
    t = both_low.transcribe(CLIP, VoiceContext("ig"))
    assert t.status == "low" and t.provider == "spitch"

    dead = VoiceRouter({"spitch": Scripted("spitch", "failed"), "muse": Scripted("muse", "failed")})
    t = dead.transcribe(CLIP, VoiceContext("ha"))
    assert t.status == "failed" and t.tried == ("spitch:failed", "muse:failed")

    only = VoiceRouter({"muse": mu})
    assert only.transcribe(CLIP, VoiceContext("yo")).provider == "muse"


# ---------------------------------------------------------------- cache, fake, env


def test_cache_records_then_replays(tmp_path: Path) -> None:
    inner = Scripted("spitch", "ok", "yes all complete")
    path = tmp_path / "t.json"
    live = CachedTranscriber(inner, path)
    ctx = VoiceContext("en", ("Sunrise",))
    assert live.transcribe(CLIP, ctx).text == "yes all complete"
    assert live.transcribe(CLIP, ctx).text == "yes all complete"
    assert inner.calls == 1 and live.live_calls == 1 and live.cached_calls == 1
    replay = CachedTranscriber(None, path, mode="replay", name="spitch")
    assert replay.transcribe(CLIP, ctx).text == "yes all complete"
    with pytest.raises(TranscriptCacheMiss):
        replay.transcribe(CLIP, VoiceContext("yo"))
    stored = path.read_text()
    assert "yes all complete" in stored and "RIFF" not in stored


def test_cache_does_not_record_unanswered_calls(tmp_path: Path) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=req)

    s = SpitchTranscriber(api_key=KEY, transport=httpx.MockTransport(boom), sleep=lambda _: None)
    c = CachedTranscriber(s, tmp_path / "t.json")
    assert c.transcribe(CLIP, VoiceContext()).status == "failed"
    assert len(c) == 0


def test_fake_uses_fixture_references() -> None:
    fake = FakeTranscriber()
    for c in CLIPS[:4]:
        t = fake.transcribe(va.inspect(clip_bytes(c)), VoiceContext())
        assert t.text == c["reference"] and t.fake
    assert FakeTranscriber().transcribe(CLIP, VoiceContext()).status == "failed"


def test_env_picks_fake_without_keys_and_router_with_both() -> None:
    assert isinstance(transcriber_from_env({}), FakeTranscriber)
    both = transcriber_from_env({"SPITCH_API_KEY": "a", "META_API_KEY": "b"})
    assert isinstance(both, VoiceRouter) and set(both.providers) == {"spitch", "muse"}
    only = transcriber_from_env({"SPITCH_API_KEY": "a", "MIMI_VOICE": "spitch"})
    assert isinstance(only, SpitchTranscriber)
    assert isinstance(
        transcriber_from_env({"META_API_KEY": "b", "MIMI_VOICE": "fake"}), FakeTranscriber
    )


# ---------------------------------------------------------------- WER


def test_wer() -> None:
    assert wer("Na 4 carton, no be 6.", "na four carton no be six") == 0.0
    assert wer("yes o", "yes oh") == 0.0
    assert wer("a b c d", "a x c") == pytest.approx(0.5)
    assert wer("Ẹ ṣé o", "e se o") == pytest.approx(2 / 3)
    assert wer("Ẹ ṣé o", "e se o", folded=True) == 0.0
    assert normalise("Yes, SIR!") == ["yes", "sir"]
    assert corpus_wer([("a b", "a b"), ("c d", "x")]) == pytest.approx(0.5)
