"""The e2e replay does not depend on the day it runs (fix/e2e-replay-date).

The e2e went red on 29 Oct 2026 with no code change: a reused payment
reference names the earlier proof with the day its intent row was written
(the store's wall clock), so the shop-020 screenshot request changed day by
day, missed its recorded answer, and the credit reject turn after it was
lost too. The store now reads one wall-clock seam (``mimi.clock``), and the
e2e pins it to the recording day in the runner and every worker
(``mimi.api.e2e.clock``). Production reads the real clock and refuses to
start pinned.

Each test fails on the code before the fix.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_api_helpers import KEYS, NOW, SCREENS, T, W, _screenshot, script

from mimi import clock
from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import ConfigError, Settings, parse_keys
from mimi.api.e2e import runner as e2e_runner
from mimi.api.e2e import worker as e2e_worker
from mimi.api.e2e.clock import ENV, RECORDED_ON, run_clock
from mimi.api.helpers import HelperDeps
from mimi.api.media import LocalMedia
from mimi.harness.gateway import ModelTurn, ScriptedModel
from mimi.models.fake import FakeModel
from mimi.proof.reading import fake_reader
from mimi.receipts.verdict import fake_verdict
from mimi.stock.answers import fake_answer
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store.queue import TurnQueue
from mimi.store.util import now_iso

ROOT = Path(__file__).resolve().parents[1]
DAY_A = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
DAY_B = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
RECORDED = f"{RECORDED_ON:%Y-%m-%d}"


@pytest.fixture(autouse=True)
def _real_clock_after() -> Any:
    yield
    clock.set_source(None)
    os.environ.pop(ENV, None)


class _Seen:
    """The scripted helper, keeping every tool result it was shown."""

    def __init__(self) -> None:
        self.tool_results: list[str] = []

    def __call__(self, messages: Any, tools: Any) -> ModelTurn:
        if messages and messages[-1].get("role") == "tool":
            self.tool_results.append(str(messages[-1]["content"]))
        return script(messages, tools)


def _reuse_scene(tmp: Path) -> list[str]:
    """Two shops send the same genuine screenshot: the second is checked
    against the first one's proof. Returns what the model was shown."""
    url = f"sqlite:///{tmp / 'scene.db'}"
    engine = make_engine(url)
    ensure_schema(engine)
    seed_world(engine, W)
    truth = {r["sha256"]: r["truth"] for r in SCREENS.values()}
    seen = _Seen()
    deps = HelperDeps(vision=FakeModel(fake_reader(truth)), judge=FakeModel(fake_verdict),
                      stock=FakeModel(fake_answer), media=LocalMedia(ROOT / "eval"))
    agent = Agent(engine, models=[ScriptedModel(seen)], deps=deps, clock=lambda: NOW,
                  worker_id="t")
    keys = parse_keys(f"{T}:owner:{KEYS['owner']},{T}:channel:{KEYS['chan']}")
    app = create_app(Settings(database_url=url, keys=keys, workers=0), agent=agent, workers=0)
    with TestClient(app) as c:
        _screenshot(c, "g03", "shop-038", "r1")
        _screenshot(c, "g03", "shop-020", "r2")
    engine.dispose()
    return [t for t in seen.tool_results if "reference already used" in t]


def _machine_day(monkeypatch: pytest.MonkeyPatch, day: datetime) -> None:
    """Pretend the machine's real clock reads ``day`` (and ticks)."""
    m0, real = time.monotonic(), day.timestamp()
    monkeypatch.setattr(time, "time", lambda: real + (time.monotonic() - m0))


def test_a_reused_reference_shows_the_model_the_store_day(tmp_path: Path) -> None:
    """The root cause: the model-facing text carries the store's day, so the
    same scene replayed on two days sends two different requests."""
    shown = []
    for i, day in enumerate((DAY_A, DAY_B)):
        (tmp_path / str(i)).mkdir()
        with clock.pinned(day):
            shown.append(_reuse_scene(tmp_path / str(i)))
    assert shown[0] and shown[1]
    assert "2026-10-30)" in shown[0][0] and "2026-11-06)" in shown[1][0]


def test_the_e2e_clock_shows_the_model_the_same_text_on_any_day(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shown = []
    for i, day in enumerate((DAY_A, DAY_B)):
        _machine_day(monkeypatch, day)
        (tmp_path / str(i)).mkdir()
        with run_clock():
            shown.append(_reuse_scene(tmp_path / str(i)))
    assert shown[0] and shown[0] == shown[1]
    assert f"{RECORDED})" in shown[0][0]


def test_production_reads_the_real_clock_and_refuses_a_pinned_one(
        monkeypatch: pytest.MonkeyPatch) -> None:
    assert not clock.is_pinned()
    assert abs(clock.wall() - time.time()) < 1
    assert abs(datetime.fromisoformat(now_iso()).timestamp() - time.time()) < 1
    keys = parse_keys(f"{T}:owner:{KEYS['owner']}")
    prod = Settings(env="production", keys=keys, has_model_key=True,
                    database_url="postgresql://x/y")
    monkeypatch.setenv("MIMI_PHONE_PSEUDONYM_KEY", "test-only-pseudonym-key")
    prod.check()
    with clock.pinned(RECORDED_ON), pytest.raises(ConfigError, match="pinned"):
        prod.check()
    prod.check()  # the real clock is back


def test_the_pin_keeps_elapsed_time_and_is_restored() -> None:
    with run_clock() as start:
        assert clock.is_pinned() and now_iso()[:10] == RECORDED
        assert float(os.environ[ENV]) == start
        a = clock.wall()
        time.sleep(0.05)
        assert 0.04 < clock.wall() - a < 5  # leases and windows keep real time
    assert not clock.is_pinned() and ENV not in os.environ


def test_queue_leases_read_the_same_clock(tmp_path: Path) -> None:
    """Leases were stamped on ``time.time`` (bound at import): under a pinned
    store clock the runner never saw a SIGKILLed worker's lease lapse."""
    engine = make_engine(f"sqlite:///{tmp_path / 'q.db'}")
    ensure_schema(engine)
    q = TurnQueue(engine, lease_s=3.0)
    with run_clock():
        assert f"{datetime.fromtimestamp(q.clock(), UTC):%Y-%m-%d}" == RECORDED
        assert abs(q.clock() - clock.wall()) < 1
    engine.dispose()


def test_a_worker_process_continues_the_runners_clock() -> None:
    """A worker (a new process, restarted after each SIGKILL) takes the run
    start from the runner: same day, and never behind the runner."""
    with run_clock():
        parent = clock.wall()
        out = subprocess.run(
            [sys.executable, "-c",
             "import json; from mimi import clock; from mimi.api.e2e.clock import pin_worker; "
             "from mimi.store.util import now_iso; pin_worker(); "
             "print(json.dumps([clock.wall(), now_iso()]))"],
            env={**os.environ}, capture_output=True, text=True, check=True, cwd=ROOT)
    child, iso = json.loads(out.stdout)
    assert iso[:10] == RECORDED
    assert parent <= child < parent + 60


class _Stop(Exception):
    pass


def test_the_e2e_runner_and_worker_both_run_pinned(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The entry points themselves pin: a worker that forgot to would stamp
    rows (and leases) on the real day."""
    _machine_day(monkeypatch, DAY_B)
    days: dict[str, str] = {}

    def runner_body(**_: Any) -> Any:
        days["runner"] = now_iso()[:10]
        raise _Stop

    def worker_body(*_: Any, **__: Any) -> Any:
        days["worker"] = now_iso()[:10]
        raise _Stop

    monkeypatch.setattr(e2e_runner, "_collect", runner_body)
    monkeypatch.setattr(e2e_worker, "build_agent", worker_body)
    with pytest.raises(_Stop):
        e2e_runner.collect(live=False, cache_dir=tmp_path, workdir=tmp_path)
    assert now_iso()[:10] == "2026-11-06" and not clock.is_pinned()  # restored
    with pytest.raises(_Stop):
        e2e_worker.run("sqlite://", live=False, cache_dir=tmp_path, worker_id="w")
    assert days == {"runner": RECORDED, "worker": RECORDED}


STAMPING = ("mimi/store", "mimi/api/agent.py", "mimi/api/app.py", "mimi/api/steering.py",
            "mimi/api/demo.py", "mimi/api/e2e/runner.py")


def test_stamping_code_reads_only_the_clock_seam() -> None:
    """One clock for every stamp and comparison: a direct ``time.time`` or
    ``datetime.now`` in the store or the API agent would stamp (or compare) on
    a second clock, which is how the lease bug above happened."""
    bad = []
    for root in STAMPING:
        p = ROOT / root
        for f in ([p] if p.is_file() else sorted(p.rglob("*.py"))):
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if re.search(r"\btime\.time\b|datetime\.now\(|date\.today\(", line):
                    bad.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not bad, bad
