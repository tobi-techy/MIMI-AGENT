"""Proofs of concept from the outside code review of main.

Every case in ``mimi.harness.review_pocs.POCS`` is a real attack or failure
run against the code (the same ones the harness eval's security suite runs).
Each failed on main before its fix; each must hold now. The API-level cases
below need the HTTP agent, which the harness may not import.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.harness.gateway import ScriptedModel, say
from mimi.harness.review_pocs import POCS
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.world.loader import load_world

W = load_world()


@pytest.mark.parametrize("cid", sorted(POCS))
def test_review_poc_holds(cid: str) -> None:
    category, fn = POCS[cid]
    held, detail = fn()
    assert held, f"{cid} ({category}): {detail}"


def _agent(store_url: str, model: Any, *, shadow: str = "") -> Any:
    from mimi.api.agent import Agent
    from mimi.api.helpers import build_helpers
    from mimi.harness.policy import GoPolicy
    from mimi.store import SqlGo

    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    policy = GoPolicy()
    helpers = dict(build_helpers(policy, go=SqlGo(engine)))
    if shadow:
        helpers[shadow] = dataclasses.replace(helpers[shadow], shadow=True)
    return Agent(engine, models=[model], helpers=helpers, policy=policy,
                 worker_id="poc-worker"), engine


def test_a_shadow_helper_sends_and_applies_nothing_through_the_api(store_url: str) -> None:
    """Shadow mode end to end: the turn runs, Go records the intents as
    shadowed, and no message, approval or credit row is written."""
    from mimi.harness.inbound import Inbound, Payload

    inv = next(r.delivery_id for r in W.receivables
               if r.shop_id == "shop-001" and r.status == "overdue")

    def script(messages: Any, tools: Any) -> Any:
        if messages[-1]["role"] == "tool":
            return say("done")
        return say("", ("send_reminder", {"shop_id": "shop-001", "invoice_id": inv,
                                          "tone": "firm"}),
                   ("send_to_user", {"channel": "shop_whatsapp", "shop_id": "shop-001",
                                     "kind": "reply", "body": "We have your balance.",
                                     "ends_turn": True}))

    agent, engine = _agent(store_url, ScriptedModel(script), shadow="collector")
    shop = W.shop("shop-001")
    agent.submit(W.tenant_id, Inbound(
        source="shop_message", org_id=W.tenant_id, external_id="wamid.shadow.1",
        received_at=_now(),
        payload=Payload(from_phone=shop.phone, text="how much I owe?")))
    while agent.process() is not None:
        pass
    with engine.connect() as conn:
        outs = conn.execute(sa.select(s.messages.c.kind, s.messages.c.status).where(
            s.messages.c.direction == "out")).all()
        intents = conn.execute(sa.select(s.intents.c.tool, s.intents.c.outcome)).all()
        aprs = conn.execute(sa.select(sa.func.count()).select_from(s.approvals)).scalar()
    assert intents, "the shadow run proposed nothing; the PoC did not run"
    assert {o for _, o in intents} == {"shadowed"}, intents
    assert not aprs
    assert not [o for o in outs if o.kind in ("reply", "send_reminder", "reminder")], outs


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


def test_a_shops_own_thread_keeps_its_prefix_when_an_event_names_no_shop() -> None:
    """The prefix re-pins when the shop changes, but a shop's own thread names
    its shop: a run there with no shop on the event (an approval answered)
    keeps the pinned bytes, so the provider's cache keeps hitting."""
    from datetime import UTC, datetime

    from mimi.harness.collector import collector_helper, default_registry
    from mimi.harness.context import ContextBuilder
    from mimi.harness.go import FakeGo
    from mimi.harness.policy import GoPolicy
    from mimi.harness.types import RunEvent

    at = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
    b = ContextBuilder(book=FakeGo(W), clock=lambda: at)
    helper = collector_helper(default_registry(), GoPolicy())
    thread = f"{W.tenant_id}:shop:shop-011"
    first = b.build(RunEvent(event_id="e1", tenant_id=W.tenant_id, thread_id=thread,
                             wake_source="shop_message", text="hi", sender="shop",
                             shop_id="shop-011", at=at), helper, "collector/collections")
    second = b.build(RunEvent(event_id="e2", tenant_id=W.tenant_id, thread_id=thread,
                              wake_source="approval_answered", text="", sender="system",
                              shop_id="", at=at), helper, "collector/collections")
    assert second.manifest.prefix_reused
    assert second.prefix.prefix_hash == first.prefix.prefix_hash


# ---------------------------------------------------- Medium: production config
# Each of these started on main before its fix (SQLite with the escape variable,
# the Makefile's demo keys, and `make api` filling both in under MIMI_ENV=production).

DEMO_KEYS = "coral-lane:owner:local-owner-key-0001,coral-lane:channel:local-channel-key-001"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prod_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_PHONE_PSEUDONYM_KEY", "poc-" + "p" * 32)


def test_poc_production_refuses_sqlite_even_with_the_old_escape(
        prod_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from mimi.api.config import ConfigError, Settings, parse_keys

    monkeypatch.setenv("MIMI_ALLOW_SQLITE_IN_PRODUCTION", "1")
    with pytest.raises(ConfigError, match="SQLite"):
        Settings(env="production", keys=parse_keys("coral-lane:owner:" + "k" * 32),
                 has_model_key=True, database_url="sqlite:///./mimi.db").check()


@pytest.mark.parametrize("keys", [DEMO_KEYS, "coral-lane:owner:local-owner-key-0001",
                                  "coral-lane:channel:local-channel-key-001"])
def test_poc_production_refuses_the_demo_keys(prod_env: None, keys: str) -> None:
    from mimi.api.config import ConfigError, Settings, parse_keys

    with pytest.raises(ConfigError, match="demo key"):
        Settings(env="production", keys=parse_keys(keys), has_model_key=True,
                 database_url="postgresql://poc/db").check()


def test_production_with_real_settings_still_starts(prod_env: None) -> None:
    from mimi.api.config import Settings, parse_keys

    Settings(env="production", keys=parse_keys("coral-lane:owner:" + "k" * 32),
             has_model_key=True, database_url="postgresql://poc/db").check()


def _makefile_api_env() -> str:
    """The Makefile's ``API_ENV`` recipe prefix as the shell sees it (no make needed)."""
    lines = (ROOT / "Makefile").read_text().splitlines()
    n = next(i for i, line in enumerate(lines) if line.startswith("API_ENV ="))
    body = [lines[n].split("=", 1)[1]]
    while lines[n].rstrip().endswith("\\"):
        n += 1
        body.append(lines[n])
    return " ".join(b.rstrip().rstrip("\\").strip() for b in body).replace("$$", "$")


def _run_api_env(**env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items()
            if k not in ("DATABASE_URL", "MIMI_DB_URL", "MIMI_API_KEYS", "MIMI_ENV")}
    script = _makefile_api_env() + ' echo "db=$DATABASE_URL keys=$MIMI_API_KEYS"'
    return subprocess.run(["sh", "-c", script], env={**base, **env}, capture_output=True,
                          text=True, timeout=30)


def test_poc_make_api_fills_no_defaults_in_production() -> None:
    proc = _run_api_env(MIMI_ENV="production")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "local-owner-key" not in out and "db=sqlite" not in out


def test_make_api_keeps_local_defaults_in_dev_and_passes_prod_settings() -> None:
    dev = _run_api_env()
    assert dev.returncode == 0 and "db=sqlite" in dev.stdout and "local-owner-key" in dev.stdout
    prod = _run_api_env(MIMI_ENV="production", DATABASE_URL="postgresql://poc/db",
                        MIMI_API_KEYS="coral-lane:owner:" + "k" * 32)  # pragma: allowlist secret
    assert prod.returncode == 0 and "db=postgresql://poc/db" in prod.stdout
