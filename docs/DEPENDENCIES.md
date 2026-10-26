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
| `sqlalchemy` | MIT | `mimi/runtime/sql_store.py`, `mimi/receipts/inbox.py`, sim `--db` |
| `alembic` | MIT | migrations `0001_runtime_jobs`, `0002_shop_inbox` |
| `httpx` | BSD-3-Clause | `mimi/models/meta.py` (Meta Model API client) |
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

## Development and CI only

| Package or tool | Licence | Used by |
|---|---|---|
| `pytest` | MIT | tests |
| `ruff`, `mypy` | MIT | lint and type checks |
| `detect-secrets` 1.5.0 | Apache-2.0 | CI secret scan, `make secrets` |
| `gitleaks` | MIT | CI history scan (binary downloaded in the workflow) |

## Services

Meta Model API (`https://api.meta.ai/v1`): `muse-spark-1.3` and
`muse-spark-1.3-contributor`, used under Meta's terms for the hackathon. No
other external service. Offline runs and CI use the deterministic FakeModel
and the recorded answers, and make no network call.
