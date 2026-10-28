"""One Africa/Lagos source (RAI-308).

PoC first, on main (02023d4): the API read a ``received_at`` with no zone as
UTC, so a WhatsApp message at 23:30 Lagos time on Monday 26 October landed on
Tuesday 27 October in Lagos. ``values_eval`` stamped the machine's zone, and
fifteen modules built their own Lagos or +1 zone.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.tz import LAGOS, assume_lagos, lagos, lagos_day

ROOT = Path(__file__).resolve().parents[1] / "mimi"


@pytest.mark.parametrize(("wall", "day"), [("2026-10-26T23:30:00", date(2026, 10, 26)),
                                           ("2026-10-27T00:30:00", date(2026, 10, 27))])
def test_a_zone_less_time_is_a_lagos_wall_clock_time(wall: str, day: date) -> None:
    at = assume_lagos(datetime.fromisoformat(wall))
    assert lagos_day(at) == day and at.utcoffset().total_seconds() == 3600  # type: ignore[union-attr]


def test_times_with_a_zone_keep_their_instant() -> None:
    utc = datetime(2026, 10, 26, 22, 30, tzinfo=UTC)
    assert lagos(utc).hour == 23 and lagos_day(utc) == date(2026, 10, 26)
    assert assume_lagos(utc) == utc
    assert lagos_day(datetime(2026, 10, 26, 23, 30, tzinfo=UTC)) == date(2026, 10, 27)


def test_a_naive_time_is_refused_where_input_is_not_expected() -> None:
    with pytest.raises(ValueError, match="without a zone"):
        lagos(datetime(2026, 10, 26, 23, 30))


def test_the_zone_prints_as_before() -> None:
    at = datetime(2026, 10, 26, 23, 30, tzinfo=LAGOS)
    assert at.isoformat() == "2026-10-26T23:30:00+01:00" and at.tzname() == "WAT"


@pytest.mark.parametrize(("wall", "day"), [("2026-10-26T23:30:00", "2026-10-26"),
                                           ("2026-10-27T00:30:00", "2026-10-27")])
def test_poc_the_api_reads_a_zone_less_received_at_as_lagos(
        store_url: str, wall: str, day: str) -> None:
    from fastapi.testclient import TestClient

    from mimi.api.agent import Agent
    from mimi.api.app import create_app
    from mimi.api.config import Settings, parse_keys
    from mimi.harness.gateway import ScriptedModel, say
    from mimi.store import ensure_schema, make_engine, seed_world
    from mimi.store import schema as s
    from mimi.world import load_world

    w = load_world()
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, w)
    agent = Agent(engine, models=[ScriptedModel(lambda m, t: say("ok"))], worker_id="tz")
    key = "tz-test-channel-key-0001"  # pragma: allowlist secret
    app = create_app(Settings(keys=parse_keys(f"{w.tenant_id}:channel:{key}"), wait_s=1),
                     agent, workers=0)
    with TestClient(app) as c:
        r = c.post("/v1/turns?wait=false", headers={"Authorization": f"Bearer {key}"},
                   json={"message_id": f"wamid.tz.{wall}", "phone": w.shops[0].phone,
                         "text": "hi", "received_at": wall})
        assert r.status_code in (200, 202), r.text
    with engine.connect() as conn:
        got = conn.execute(sa.select(s.inbound_messages.c.received_at)).scalars().all()
    engine.dispose()
    [at] = got
    assert lagos_day(datetime.fromisoformat(at)).isoformat() == day, at


def _zone_builders(path: Path) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", getattr(node.func, "attr", ""))
        src = ast.unparse(node)
        if name == "ZoneInfo" or (name == "timezone" and "timedelta" in src):
            out.append(src)
    return out


def test_no_module_builds_its_own_lagos_zone() -> None:
    found: dict[str, Any] = {}
    for path in ROOT.rglob("*.py"):
        if path == ROOT / "tz.py" or "fixtures" in path.parts:
            continue
        hits = _zone_builders(path)
        if hits:
            found[str(path.relative_to(ROOT))] = hits
    assert not found, f"use mimi.tz instead: {found}"


def test_values_eval_stamps_lagos_time() -> None:
    src = (ROOT / "receipts" / "values_eval.py").read_text(encoding="utf-8")
    assert "datetime.now().astimezone()" not in src
