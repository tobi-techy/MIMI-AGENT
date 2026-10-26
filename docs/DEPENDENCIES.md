# Dependencies

Everything this repo's code imports, with licences. Nothing here imports code
written before the hackathon: the one small piece the window's code used from
earlier work (a two-field tenant and shop scope, and the list of intent types
a runner accepts) was written fresh as `mimi/types.py`, and the runtime now
starts with no intent types of its own. The module rules test
(`tests/test_architecture.py`) keeps `mimi.runtime` free of anything above it.

## Runtime packages

| Package | Licence | Used by |
|---|---|---|
| `sqlalchemy` | MIT | `mimi/runtime/sql_store.py`, `mimi/receipts/inbox.py`, `mimi/voice/prefs.py`, sim `--db` |
| `alembic` | MIT | migrations `0001_runtime_jobs` to `0004_voice_consent` |
| `httpx` | BSD-3-Clause | `mimi/models/meta.py` (Meta Model API client), `mimi/voice/` (Spitch and Muse Voice Transcribe clients) |
| `pydantic` | MIT | `mimi/receipts/verdict.py` (typed verdict) |

Optional extras:

| Package | Extra | Licence | Used by |
|---|---|---|---|
| `opentelemetry-api` | `otel` | Apache-2.0 | `mimi/runtime/tracing.py` `OtelStepTracer`, imported only when that tracer is built; the sim and tests use `RecordingStepTracer` |
| `psycopg2-binary` | `postgres` | LGPL with exceptions (dynamic use only, unmodified) | only when the sim or alembic points at Postgres |

Their transitive packages: `httpcore` (BSD-3-Clause), `h11` (MIT), `anyio`
(MIT), `idna` (BSD-3-Clause), `certifi` (MPL-2.0), `pydantic-core` (MIT),
`annotated-types` (MIT), `typing-extensions` (PSF-2.0), `typing-inspection`
(MIT), `greenlet` (MIT AND PSF-2.0), `mako` (MIT), `markupsafe`
(BSD-3-Clause).

## System tools

| Tool | Licence | Used by |
|---|---|---|
| `ffmpeg`, `ffprobe` | LGPL-2.1 or later (GPL builds exist; we only run the binary, never link it) | `mimi/voice/audio.py`: reading MP3 and M4A, converting to 16 kHz WAV for Muse Voice Transcribe, encoding voice replies as Ogg/Opus; `scripts/make_voice_fixtures.py`: noise and encoding. Installed in CI with apt. Ogg/Opus and WAV input work without it; without it a voice reply is not made and the text goes alone |
| IANA time zone data | public domain | `zoneinfo` (standard library) for Africa/Lagos in `mimi/voice/promise.py`; the system copy on Linux and macOS (on Windows, `pip install tzdata`) |

Not used: the official `spitch` Python SDK (Apache-2.0, checked on PyPI,
version 1.50.0). The Spitch adapter calls the REST API with `httpx`, like the
Meta client, so there is one HTTP stack and tests use `httpx.MockTransport`.
espeak-ng and piper were not used either; the fixtures are made with Spitch
text to speech.

## Development and CI only

| Package or tool | Licence | Used by |
|---|---|---|
| `pytest` | MIT | tests |
| `ruff`, `mypy` | MIT | lint and type checks |
| `detect-secrets` 1.5.0 | Apache-2.0 | CI secret scan, `make secrets` |
| `gitleaks` | MIT | CI history scan (binary downloaded in the workflow) |

## Services

Meta Model API (`https://api.meta.ai/v1`): `muse-spark-1.3`,
`muse-spark-1.3-contributor` and `muse-voice-transcribe-1.0`, used under
Meta's terms for the hackathon.

Spitch (`https://api.spitch.app/v1`), a third-party paid API, usage-based:
speech to text (USD 0.00042 per second of audio) for voice notes, and text to
speech (USD 0.0014 per second) for the synthetic fixtures and for voice
replies to shops that opted in. Every
call sends `X-Data-Retention: false`, so Spitch stores no copy of the audio
or the output. Key: `SPITCH_API_KEY`, from the shell only.

Offline runs and CI use the deterministic FakeModel, the fake transcriber
and the recorded answers, and make no network call.
