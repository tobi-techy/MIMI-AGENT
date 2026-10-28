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
| `sqlalchemy` | MIT | `mimi/runtime/sql_store.py`, `mimi/receipts/inbox.py`, `mimi/voice/prefs.py`, sim `--db`, `mimi/store/` (the API's durable store) |
| `alembic` | MIT | migrations `0001_runtime_jobs` to `0006_api_store` |
| `httpx` | BSD-3-Clause | `mimi/models/meta.py` (Meta Model API client), `mimi/harness/gateway.py` (tool-calling client), `mimi/voice/` (Spitch and Muse Voice Transcribe clients) |
| `fastapi` | MIT | `mimi/api/app.py` only (the HTTP API); a test keeps every other module free of web frameworks |
| `uvicorn` | BSD-3-Clause | `python -m mimi.api serve` (the ASGI server) |
| `pydantic` | MIT | `mimi/receipts/verdict.py` (typed verdict), `mimi/proof/reading.py`, `mimi/stock/answers.py` (typed stock answer), `mimi/harness/` (tool inputs and outputs, events, intents, run results, the front-door envelope, route decisions, context manifests) |

Optional extras:

| Package | Extra | Licence | Used by |
|---|---|---|---|
| `opentelemetry-api` | `otel` | Apache-2.0 | `mimi/runtime/tracing.py` `OtelStepTracer`, imported only when that tracer is built; the sim and tests use `RecordingStepTracer` |
| `langfuse` 5.0.4 (pinned) | `langfuse` | MIT | `mimi/observe/` (traces, datasets, experiments), `scripts/trace_smoke.py`, `scripts/trace_stock.py`, `experiments/proof_experiment.py`; imported only when the `LANGFUSE_*` keys are set (the tests use a mock client) |
| `psycopg2-binary` | `postgres` | LGPL with exceptions (dynamic use only, unmodified) | only when the sim, alembic or the API store points at Postgres (the Docker image installs it) |

Their transitive packages: `httpcore` (BSD-3-Clause), `h11` (MIT), `anyio`
(MIT), `idna` (BSD-3-Clause), `certifi` (MPL-2.0), `pydantic-core` (MIT),
`annotated-types` (MIT), `typing-extensions` (PSF-2.0), `typing-inspection`
(MIT), `greenlet` (MIT AND PSF-2.0), `mako` (MIT), `markupsafe`
(BSD-3-Clause). The API adds `starlette` (BSD-3-Clause, under FastAPI),
`annotated-doc` (MIT), `click` (BSD-3-Clause, under uvicorn) and
`opentelemetry-api` (Apache-2.0, which recent FastAPI requires), besides
`h11`, `anyio`, `pydantic` and `typing-extensions` above. Tests use
FastAPI's `TestClient` over `httpx`.

The `langfuse` extra brings: `opentelemetry-api`, `opentelemetry-sdk`,
`opentelemetry-exporter-otlp-proto-http`, `opentelemetry-exporter-otlp-proto-common`,
`opentelemetry-exporter-otlp-common`, `opentelemetry-exporter-http-transport`,
`opentelemetry-proto` and `opentelemetry-semantic-conventions` (all
Apache-2.0), `protobuf` (BSD-3-Clause), `googleapis-common-protos`
(Apache-2.0), `requests` (Apache-2.0) with `urllib3` and
`charset-normalizer` (MIT), `backoff` (MIT), `wrapt` (BSD-2-Clause) and
`packaging` (Apache-2.0 OR BSD-2-Clause), besides `httpx` and `pydantic`
above. `scripts/langfuse_monitor.py` and `scripts/langfuse_setup.py` use only
`httpx`.

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
| `pillow` >= 10.1 | MIT-CMU (HPND) | draws the synthetic payment screenshots (`mimi/proof/render.py`, `scripts/make_proof_fixtures.py`, sim `--screenshots`); Mimi itself never draws or stores an image |
| DejaVu fonts (system package, e.g. `fonts-dejavu-core`) | Bitstream Vera / DejaVu free licence | text in the drawn screenshots; not copied into the repo (Pillow's default font is the fallback) |
| `gitleaks` | MIT | CI history scan (binary downloaded in the workflow) |

Container: the `Dockerfile` starts from the official `python:3.12-slim` image
(Debian packages under their own free licences; Python under the PSF
licence). Nothing is added to it beyond this package and its dependencies.

## Services

Meta Model API (`https://api.meta.ai/v1`): `muse-spark-1.3` (text, and
vision input for payment screenshots), `muse-spark-1.3-contributor` and
`muse-voice-transcribe-1.0`, used under Meta's terms for the hackathon.

Spitch (`https://api.spitch.app/v1`), a third-party paid API, usage-based:
speech to text (USD 0.00042 per second of audio) for voice notes, and text to
speech (USD 0.0014 per second) for the synthetic fixtures and for voice
replies to shops that opted in. Every
call sends `X-Data-Retention: false`, so Spitch stores no copy of the audio
or the output. Key: `SPITCH_API_KEY`, from the shell only.

Langfuse Cloud (EU, `https://cloud.langfuse.com`), optional: traces,
datasets, experiments, evaluators and a dashboard for the project, on the
Hobby plan. Keys: `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`, from the
shell only. The optional `langfuse/experiment-action` GitHub Action is
pinned by commit (v1.0.12) in a manual, off-by-default workflow.

The router's fast classifier (`mimi/harness/fastclass.py`) is a naive Bayes
model written here in plain Python (no new dependency). Its training set
(`mimi/harness/data/router_train.jsonl`, 2,708 lines) was written by
`muse-spark-1.3-contributor` from synthetic prompts only (no real shop text),
with real bank and fintech names replaced by the fictional ones.

Offline runs and CI use the deterministic FakeModel, the fake transcriber
and the recorded answers, and make no network call.
