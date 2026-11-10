"""The numbers a founder asks for: one snapshot, no vendor, no private text.

``mimi.observe.metrics`` reads the store with plain SQL and renders Prometheus
text. These tests pin what the page says, not how it looks: the handful of
questions in ``docs/SLO.md``, labelled by helper and tenant only, and a scrape
endpoint that is shut unless a token was configured.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from mimi.api.agent import Agent
from mimi.harness.inbound import Inbound, Payload
from mimi.observe import metrics
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.world.loader import load_world

W = load_world()
T = "coral-lane"
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # 10:00 WAT
SAMPLE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? -?[0-9][0-9eE.+-]*$")


@pytest.fixture(autouse=True)
def _cheap_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Balance questions run on code: these tests are about the numbers."""
    monkeypatch.setenv("MIMI_PLAIN_CODE", "balance")


@pytest.fixture()
def engine() -> Any:
    e = make_engine("sqlite://")
    ensure_schema(e)
    seed_world(e, W, daily_cap_kobo=50_000)
    return e


def run_one(engine: Any, ext: str = "metrics.1", text: str = "How much I owe now?") -> str:
    from mimi.api.offline import OfflineModel

    agent = Agent(engine, models=[OfflineModel()], router_model=None,
                  worker_id="w-metrics", clock=lambda: NOW)
    v = agent.submit(T, Inbound(source="shop_message", org_id=T, external_id=ext,
                                received_at=NOW,
                                payload=Payload(text=text,
                                                from_phone=W.shop("shop-033").phone)))
    assert agent.process(str(v.turn_id)) == v.turn_id
    return str(v.turn_id)


def names(text: str) -> set[str]:
    return {line.split("{")[0].split(" ")[0] for line in text.splitlines()
            if line and not line.startswith("#")}


# ---------------------------------------------------------------- the page


def test_an_empty_store_still_answers_every_question(engine: Any) -> None:
    snap = metrics.snapshot(engine, now=NOW)
    assert set(snap) >= {"generated_at", "day", "window_s", "turns", "queue", "spend",
                         "connectors", "approvals"}
    assert snap["turns"]["per_minute_5m"] == 0
    assert snap["queue"] == {"depth": 0, "leased": 0, "waiting": 0, "oldest_wait_s": None}
    assert snap["approvals"]["waiting"] == 0
    assert [row["tenant_id"] for row in snap["spend"]] == [T]
    row = snap["spend"][0]
    # N250 a day per active shop, floored at one shop: bounded, never unlimited.
    assert row["active_shops"] >= 1
    assert row["cap_kobo"] == 25_000 * row["active_shops"]
    assert row["turns_cap"] == row["cap_kobo"] // 1_642
    assert metrics.render_prometheus(snap).endswith("\n")


def test_one_turn_shows_up_in_turns_latency_and_spend(engine: Any) -> None:
    run_one(engine)
    snap = metrics.snapshot(engine, now=NOW)
    assert snap["turns"]["per_minute_5m"] > 0
    assert snap["turns"]["latency_ms"]["count"] == 1
    assert snap["turns"]["latency_ms"]["p50"] is not None
    assert snap["turns"]["finished_24h"] == 1 and snap["turns"]["failed_24h"] == 0
    assert snap["spend"][0]["spent_kobo"] == 0  # plain code buys no tokens
    assert snap["queue"]["depth"] == 0


def test_a_queued_turn_is_depth_and_age(engine: Any) -> None:
    from mimi.api.offline import OfflineModel

    agent = Agent(engine, models=[OfflineModel()], router_model=None,
                  worker_id="w-metrics", clock=lambda: NOW)
    agent.submit(T, Inbound(source="shop_message", org_id=T, external_id="metrics.q1",
                            received_at=NOW,
                            payload=Payload(text="How much I owe now?",
                                            from_phone=W.shop("shop-033").phone)))
    snap = metrics.snapshot(engine)  # the queue stamps real time, not the pinned day
    assert snap["queue"]["depth"] == 1 and snap["queue"]["waiting"] == 1
    assert 0 <= snap["queue"]["oldest_wait_s"] < 60


def test_a_failed_turn_counts_against_the_helper(engine: Any) -> None:
    turn_id = run_one(engine)
    import sqlalchemy as sa

    from mimi.store import schema as s

    with engine.begin() as conn:
        conn.execute(sa.update(s.turns).where(s.turns.c.turn_id == turn_id)
                     .values(state="failed", status="failed_error", helper="collector"))
    snap = metrics.snapshot(engine, now=NOW)
    assert snap["turns"]["failed_24h"] == 1 and snap["turns"]["failure_rate_24h"] == 1.0
    row = next(r for r in snap["turns"]["by_helper"] if r["helper"] == "collector")
    assert row["failed"] == 1 and row["turns"] == 1
    assert names(metrics.render_prometheus(snap)) >= {"mimi_helper_failure_rate",
                                                     "mimi_helper_turns"}


# ------------------------------------------------------------ the exposition


def test_the_exposition_is_parseable_prometheus_text(engine: Any) -> None:
    run_one(engine)
    text = metrics.render_prometheus(metrics.snapshot(engine, now=NOW))
    seen: set[str] = set()
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(maxsplit=3)
            assert kind in {"counter", "gauge"}
            seen.add(name)
            continue
        if line.startswith("#") or not line:
            continue
        assert SAMPLE.match(line), line
        assert line.split("{")[0].split(" ")[0] in seen, "a sample without a TYPE line"
    assert {"mimi_turns_per_minute", "mimi_turn_latency_ms", "mimi_queue_depth",
            "mimi_queue_oldest_wait_seconds", "mimi_spend_today_kobo",
            "mimi_connector_sync_error_rate", "mimi_approvals_waiting"} <= names(text)


def test_no_label_carries_a_phone_key_or_model_text(engine: Any) -> None:
    run_one(engine)
    text = metrics.render_prometheus(metrics.snapshot(engine, now=NOW))
    phone = W.shop("shop-033").phone
    assert phone not in text and phone[-7:] not in text
    assert not re.search(r"\d{10,}", text), "no long digit run in a metric"
    assert "shop-033" not in text  # helpers and tenants are labels, shops are not
    assert "Mama Sola" not in text


def test_a_label_with_a_quote_stays_one_line(engine: Any) -> None:
    line = metrics._line("mimi_x", 1, {"helper": 'odd"name\nhere'})
    assert line.count("\n") == 2 and '\\"name\\nhere' in line


# -------------------------------------------------------------- the endpoint


def call(app: Any, path: str = "/metrics", auth: str = "") -> tuple[int, bytes]:
    import asyncio

    async def go() -> tuple[int, bytes]:
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        headers = [(b"authorization", auth.encode())] if auth else []
        await app({"type": "http", "path": path, "headers": headers}, receive, send)
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent
                        if m["type"] == "http.response.body")
        return status, body

    return asyncio.run(go())


def test_the_endpoint_is_shut_without_a_token(engine: Any) -> None:
    app = metrics.asgi_app(engine, token="", open_access=False)
    assert call(app)[0] == 503
    assert call(app, path="/other")[0] == 404


def test_the_endpoint_needs_the_bearer_token(engine: Any) -> None:
    app = metrics.asgi_app(engine, token="s3cret", open_access=False)
    assert call(app)[0] == 401
    assert call(app, auth="Bearer wrong")[0] == 401
    status, body = call(app, auth="Bearer s3cret")
    assert status == 200 and b"mimi_queue_depth" in body


def test_the_standalone_server_serves_the_same_page(engine: Any) -> None:
    import threading
    import urllib.error
    import urllib.request

    run_one(engine)
    server = metrics.make_server(engine, host="127.0.0.1", port=0, token="tok")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(base + "/metrics", timeout=5)
        assert denied.value.code == 401
        req = urllib.request.Request(base + "/metrics",
                                     headers={"Authorization": "Bearer tok"})
        with urllib.request.urlopen(req, timeout=5) as res:
            body = res.read().decode()
        assert res.status == 200 and "mimi_turn_latency_ms" in body
    finally:
        server.shutdown()
        server.server_close()


def test_a_broken_store_serves_an_error_not_a_half_page() -> None:
    app = metrics.asgi_app(None, token="t", open_access=True)
    status, body = call(app, auth="Bearer t")
    assert status == 500 and b"metrics unavailable" in body


def test_percentile_is_nearest_rank() -> None:
    assert metrics.percentile([], 0.5) is None
    assert metrics.percentile([5.0], 0.95) == 5.0
    assert metrics.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert metrics.percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0
