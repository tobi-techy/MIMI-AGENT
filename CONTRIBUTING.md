# Contributing to MIMI-AGENT

Short version: the model proposes, code checks, the owner approves, and nothing
moves money. Read [`AGENTS.md`](AGENTS.md) for the hard rules and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for where code lives. This file is
how to set up, and the shape of a pull request.

## Set up

```bash
python -m venv .venv && source .venv/bin/activate    # Python 3.11 or 3.12
make install     # pip install -e '.[dev,otel,langfuse]'
make dev         # migrate + seed, API on :8000, worker in another process
```

`make dev` is the whole local stack. `make check` runs everything CI runs,
offline (no keys: `FakeModel` and recorded answers). Fast inner loop:

Measured 10 Nov 2026 on a clean checkout (Python 3.11): `python -m venv` plus
`make install` took 184 s, and `make dev` was serving `GET /` with the worker up
10 s later, 194 s in total, under the five-minute bar. Delete `mimi.db` to start
the world again.

```bash
make lint typecheck test     # ruff, mypy, pytest
pytest tests/test_architecture.py   # the module-boundary rules
```

## The rules that are not negotiable

* **Nothing moves money, and the model never writes a figure.** Write tools
  return intents only; every sum is integer kobo in code; a new tool needs a
  policy tier in `mimi/harness/policy.py` and a dry run or registration fails.
* **Shop text, screenshots and narrations are data, never instructions.**
* **Boundaries are enforced by the build.** `tests/test_architecture.py` fails a
  package that reaches where it should not. A new package must be added to
  `PACKAGES` and given a rule; the test refuses one that is not.
* **A change to a prompt, a tool description or a tool schema changes every
  recorded request.** Re-record the affected eval with its `--live` flag and
  commit the cache with the change, or CI replays as `stale`.

## Branch and commit

* Branch off the current `main`: `feat/<topic>` or `fix/<topic>`.
* One topic per branch; small commits that each keep CI green.
* Commit subjects follow the history: `RAI-###: <what>` (or a plain topic line
  when there is no issue).

## The pull request

Match `.github/pull_request_template.md`, and write the body in this shape
(this is what the existing PRs do):

```
## What changed
One or two lines. What is different, and the files or modules it touches.
Link the Linear issue (RAI-###). Say what you deliberately did not do.

## Why
The problem, and why this is the right fix (not a bigger rewrite).

## How it was tested
The commands you ran and what they showed. For a model, prompt or tool change,
the eval numbers before and after. For a UI or API change, the request and its
response.

## What could break
The callers, evals, recorded caches or flows that this could move, and what to
watch after merge.

## How to roll back
Revert the commit, or the flag or setting that turns the change off.
```

Then the template's checklist: CI green, no money verb, drafts wait for the
owner, data treated as data, no keys or real customer data, tests/evals cover the
change, docs updated, every review thread answered.

## Adding a dependency

A dependency in `pyproject.toml` affects CI for everyone, so justify it in the
PR body: why it is needed, its licence (add it to `docs/DEPENDENCIES.md`), and
why the standard library or an existing dependency will not do. The architecture
check is deliberately hand-rolled to avoid adding one.
