"""The numbers a founder asks for, computed from the store, with no vendor.

Every figure here answers a question somebody actually asks during a pilot:

| Question | Metric |
|---|---|
| Is Mimi keeping up? | turns a minute (5 min and 1 hour) |
| Is a turn fast? | turn latency p50 and p95 |
| Is work waiting? | queue depth and the age of the oldest waiting turn |
| Which helper is failing? | turns and failures by helper |
| What is this costing? | model spend today against the cap, per distributor |
| Are the connectors healthy? | connector sync error rate |
| Is the owner answering? | approvals waiting and the age of the oldest |

Design choices, so the PR can defend them against "just add OpenTelemetry":

* **Read the store you already write.** Every number comes from the tables the
  runtime already keeps (``turns``, ``approvals``, ``spend``,
  ``connector_sync_runs``), so there is no second pipeline to keep honest and
  no agent to install. Reads are indexed and bounded to one day or one short
  window.
* **No new dependency.** The output is the Prometheus text format, which every
  scraper and every hosted option reads. An OpenTelemetry collector can come
  later without changing this module's callers.
* **One tenant label, never private text.** Labels are a distributor id, a
  helper name and a window name. No shop, phone or model text is a label: a
  label is the most expensive place to leak (``docs/SLO.md``).
* **Raw SQL on purpose.** The architecture test keeps ``mimi.observe`` beside
  the runtime, so this module does not import the store; it reads the schema's
  own table and column names (pinned by ``tests/test_store.py``).

``python -m mimi.observe.metrics`` prints the snapshot; ``asgi_app`` serves it
for a scraper.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from typing import Any

import sqlalchemy as sa

from mimi.observe import allowance
from mimi.observe import spend as budget
from mimi.tz import lagos_day

WINDOW_S = 300.0
HOUR_S = 3600.0
DAY_S = 86_400.0


# ------------------------------------------------------------------ helpers


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile: no interpolation, no invented precision."""
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    rank = max(1, min(len(ordered), int(round(q * len(ordered) + 0.4999))))
    return ordered[rank - 1]


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000.0, 1)


def _iso_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except ValueError:
        return None


def _rows(engine: Any, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(sa.text(sql), params).all())


# ------------------------------------------------------------------ snapshot


def snapshot(engine: Any, *, now: datetime | None = None, window_s: float = WINDOW_S) -> dict[str, Any]:
    """Every metric, from the store, as of ``now`` (UTC; the day is Lagos)."""
    at = (now or datetime.now(UTC)).astimezone(UTC)
    today = lagos_day(at)
    day = today.isoformat()
    epoch = at.timestamp()
    since_window = epoch - window_s
    since_hour = epoch - HOUR_S
    since_day = epoch - DAY_S

    turns = _turn_stats(engine, now=epoch, since_window=since_window,
                        since_hour=since_hour, since_day=since_day)
    queue = _queue_stats(engine, now=epoch)
    spend = _spend_stats(engine, day=today)
    connectors = _connector_stats(engine, since_day=since_day, since_hour=since_hour)
    approvals = _approval_stats(engine, now=epoch)
    return {
        "generated_at": at.isoformat(),
        "day": day,
        "window_s": window_s,
        "turns": turns,
        "queue": queue,
        "spend": spend,
        "connectors": connectors,
        "approvals": approvals,
    }


def _turn_stats(engine: Any, *, now: float, since_window: float, since_hour: float,
                since_day: float) -> dict[str, Any]:
    recent = _rows(engine,
                   "SELECT turn_id, helper, state, status, created_ts, finished_at "
                   "FROM turns WHERE created_ts >= :since ORDER BY created_ts",
                   since=since_hour)
    in_window = [r for r in recent if float(r.created_ts or 0) >= since_window]
    latencies: list[float] = []
    for r in recent:
        finished = _iso_ts(r.finished_at)
        if finished is not None:
            latencies.append(max(0.0, finished - float(r.created_ts or finished)))
    day_rows = _rows(engine,
                     "SELECT turn_id, helper, state, status, created_ts, finished_at "
                     "FROM turns WHERE created_ts >= :since",
                     since=since_day)
    by_helper: dict[str, dict[str, Any]] = {}
    for r in day_rows:
        helper = str(r.helper or "-")
        row = by_helper.setdefault(helper, {"helper": helper, "turns": 0, "failed": 0,
                                            "_lat": []})
        row["turns"] += 1
        if _failed(r):
            row["failed"] += 1
        finished = _iso_ts(r.finished_at)
        if finished is not None:
            row["_lat"].append(max(0.0, finished - float(r.created_ts or finished)))
    helpers = []
    for row in sorted(by_helper.values(), key=lambda x: x["helper"]):
        lat = row.pop("_lat")
        row["p95_ms"] = _ms(percentile(lat, 0.95))
        row["failure_rate"] = round(row["failed"] / row["turns"], 4) if row["turns"] else 0.0
        helpers.append(row)
    finished_day = sum(1 for r in day_rows if _iso_ts(r.finished_at) is not None)
    failed_day = sum(1 for r in day_rows if _failed(r))
    finished_hour = sum(1 for r in recent if _iso_ts(r.finished_at) is not None)
    failed_hour = sum(1 for r in recent if _failed(r))
    return {
        "per_minute_5m": round(len(in_window) / (WINDOW_S / 60.0), 2),
        "per_minute_1h": round(len(recent) / 60.0, 2),
        "latency_ms": {"p50": _ms(percentile(latencies, 0.5)),
                       "p95": _ms(percentile(latencies, 0.95)),
                       "count": len(latencies)},
        "finished_24h": finished_day,
        "failed_24h": failed_day,
        "failure_rate_24h": round(failed_day / finished_day, 4) if finished_day else 0.0,
        "finished_1h": finished_hour,
        "failed_1h": failed_hour,
        "failure_rate_1h": round(failed_hour / finished_hour, 4) if finished_hour else 0.0,
        "by_helper": helpers,
    }


def _failed(row: Any) -> bool:
    state, status = str(row.state or ""), str(row.status or "")
    return state == "failed" or status.startswith(("failed", "poison", "stopped_denied"))


def _queue_stats(engine: Any, *, now: float) -> dict[str, Any]:
    rows = _rows(engine,
                 "SELECT state, COUNT(*) AS n, MIN(created_ts) AS oldest "
                 "FROM turns WHERE state IN ('queued', 'leased') GROUP BY state")
    depth = sum(int(r.n) for r in rows)
    waiting = [float(r.oldest) for r in rows if r.oldest is not None]
    oldest = min(waiting) if waiting else None
    leased = sum(int(r.n) for r in rows if str(r.state) == "leased")
    return {
        "depth": depth,
        "leased": leased,
        "waiting": depth - leased,
        "oldest_wait_s": round(max(0.0, now - oldest), 1) if oldest is not None else None,
    }


def _spend_stats(engine: Any, *, day: date) -> list[dict[str, Any]]:
    """Spend today against the cap the gate enforces, per distributor.

    The cap is read through ``mimi.observe.allowance``, the same function
    ``SqlGo.daily_spend`` reads, so the report and the enforcement cannot
    disagree. Turns are the derived view the owner reads; kobo is the limit.
    """
    out = []
    with engine.connect() as conn:
        tenants = [str(r.tenant_id) for r in conn.execute(sa.text(
            "SELECT tenant_id FROM tenants ORDER BY tenant_id")).fetchall()]
        spent_by_tenant = {str(r.tenant_id): int(r.kobo or 0) for r in conn.execute(
            sa.text("SELECT tenant_id, kobo FROM spend WHERE day = :day"),
            {"day": day.isoformat()}).fetchall()}
        for tenant_id in tenants:
            a = allowance.allowance(conn, tenant_id, day)
            spent = spent_by_tenant.get(tenant_id, 0)
            out.append({"tenant_id": tenant_id, "day": day.isoformat(),
                        "spent_kobo": spent, "cap_kobo": a.cap_kobo,
                        "active_shops": a.active_shops,
                        "cap_source": a.source,
                        "used_fraction": round(spent / a.cap_kobo, 4) if a.cap_kobo else None,
                        "turns_used": budget.turns_for(spent),
                        "turns_cap": a.turns})
    return out


def _connector_stats(engine: Any, *, since_day: float,
                     since_hour: float = 0.0) -> dict[str, Any]:
    """Connector health: failed syncs (any error recorded) and writes applied.

    Times are parsed, not compared as strings: an ISO stamp with and without
    microseconds does not order lexicographically.
    """
    rows = _rows(engine,
                 "SELECT errors, started_at FROM connector_sync_runs "
                 "ORDER BY started_at DESC LIMIT 5000")
    runs = failed = hour_runs = hour_failed = 0
    for r in rows:
        at = _iso_ts(r.started_at)
        if at is None:
            continue
        raw = str(r.errors or "[]")
        try:
            errs = json.loads(raw)
        except ValueError:
            errs = [raw] if raw.strip() else []
        if since_hour and at >= since_hour:
            hour_runs += 1
            hour_failed += 1 if errs else 0
        if at < since_day:
            continue
        runs += 1
        if errs:
            failed += 1
    writes = 0
    for r in _rows(engine,
                   "SELECT applied_at FROM connector_writes "
                   "ORDER BY applied_at DESC LIMIT 5000"):
        at = _iso_ts(r.applied_at)
        if at is not None and at >= since_day:
            writes += 1
    return {"sync_runs_24h": runs, "sync_failed_24h": failed,
            "sync_error_rate": round(failed / runs, 4) if runs else 0.0,
            "sync_runs_1h": hour_runs,
            "sync_failed_1h": hour_failed,
            "sync_error_rate_1h": round(hour_failed / hour_runs, 4) if hour_runs else 0.0,
            "writes_24h": writes}


def _approval_stats(engine: Any, *, now: float) -> dict[str, Any]:
    rows = _rows(engine,
                 "SELECT tenant_id, created_ts FROM approvals WHERE status = 'pending'")
    waits = [max(0.0, now - float(r.created_ts or now)) for r in rows]
    by_tenant: dict[str, int] = {}
    for r in rows:
        by_tenant[str(r.tenant_id)] = by_tenant.get(str(r.tenant_id), 0) + 1
    return {
        "waiting": len(rows),
        "oldest_wait_s": round(max(waits), 1) if waits else None,
        "waiting_by_tenant": [{"tenant_id": k, "waiting": v}
                              for k, v in sorted(by_tenant.items())],
    }


# ---------------------------------------------------------------- exposition


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _line(name: str, value: float | int | None, labels: dict[str, str] | None = None,
          *, kind: str = "gauge") -> str:
    if value is None:
        return ""
    body = ""
    if labels:
        body = "{" + ",".join(f'{k}="{_label(v)}"' for k, v in labels.items()) + "}"
    return f"# TYPE {name} {kind}\n{name}{body} {value}\n"


def render_prometheus(snap: dict[str, Any]) -> str:
    """The snapshot as Prometheus text: one line per series, no private text."""
    out: list[str] = []
    t = snap["turns"]
    q = snap["queue"]
    a = snap["approvals"]
    c = snap["connectors"]
    out.append(_line("mimi_turns_per_minute", t["per_minute_5m"], {"window": "5m"}))
    out.append(_line("mimi_turns_per_minute", t["per_minute_1h"], {"window": "1h"}))
    for name, value in (("p50", t["latency_ms"]["p50"]), ("p95", t["latency_ms"]["p95"])):
        out.append(_line("mimi_turn_latency_ms", value, {"window": "1h", "quantile": name}))
    out.append(_line("mimi_turns_finished", t["finished_24h"], {"window": "24h"}, kind="counter"))
    out.append(_line("mimi_turn_failures", t["failed_24h"], {"window": "24h"}, kind="counter"))
    out.append(_line("mimi_turn_failure_rate", t["failure_rate_24h"], {"window": "24h"}))
    out.append(_line("mimi_turns_finished", t["finished_1h"], {"window": "1h"}, kind="counter"))
    out.append(_line("mimi_turn_failure_rate", t["failure_rate_1h"], {"window": "1h"}))
    for row in t["by_helper"]:
        labels = {"helper": row["helper"], "window": "24h"}
        out.append(_line("mimi_helper_failure_rate", row["failure_rate"], labels))
        out.append(_line("mimi_helper_turns", row["turns"], labels, kind="counter"))
        out.append(_line("mimi_helper_latency_ms", row["p95_ms"], {**labels,
                                                                   "quantile": "p95"}))
    out.append(_line("mimi_queue_depth", q["depth"]))
    out.append(_line("mimi_queue_leased", q["leased"]))
    # Nothing waiting is an age of zero, not a missing series: an alert on
    # "oldest > 10m" must not go quiet when the queue empties.
    out.append(_line("mimi_queue_oldest_wait_seconds", q["oldest_wait_s"] or 0))
    for row in snap["spend"]:
        labels = {"tenant": row["tenant_id"], "day": row["day"]}
        out.append(_line("mimi_spend_today_kobo", row["spent_kobo"], labels,
                         kind="counter"))
        out.append(_line("mimi_spend_cap_kobo", row["cap_kobo"], labels))
        out.append(_line("mimi_spend_used_fraction", row["used_fraction"], labels))
        # The owner's unit: model turns at the measured average cost. Money is
        # what the gate enforces; this is the same cap said in turns.
        out.append(_line("mimi_spend_turns_used", row["turns_used"], labels,
                         kind="counter"))
        out.append(_line("mimi_spend_turns_cap", row["turns_cap"], labels))
        out.append(_line("mimi_spend_active_shops", row["active_shops"], labels))
    out.append(_line("mimi_connector_sync_error_rate", c["sync_error_rate"],
                     {"window": "24h"}))
    out.append(_line("mimi_connector_sync_error_rate", c["sync_error_rate_1h"],
                     {"window": "1h"}))
    out.append(_line("mimi_connector_sync_runs", c["sync_runs_1h"], {"window": "1h"},
                     kind="counter"))
    out.append(_line("mimi_connector_sync_runs", c["sync_runs_24h"], {"window": "24h"},
                     kind="counter"))
    out.append(_line("mimi_connector_writes", c["writes_24h"], {"window": "24h"},
                     kind="counter"))
    out.append(_line("mimi_approvals_waiting", a["waiting"]))
    out.append(_line("mimi_approvals_oldest_wait_seconds", a["oldest_wait_s"] or 0))
    return "".join(out)


# ------------------------------------------------------------------- serving


def asgi_app(engine: Any, *, path: str = "/metrics", token: str | None = None,
             open_access: bool | None = None) -> Any:
    """A tiny ASGI app serving ``/metrics``; mount it, no framework needed.

    It refuses to serve without ``token`` (``MIMI_METRICS_TOKEN``) unless the
    operator explicitly opened it (``MIMI_METRICS_OPEN=1``): tenant ids in a
    label are cheap to leak and this endpoint is for a scraper on a private
    network, not the internet.
    """
    import os

    tok = str(token if token is not None else os.environ.get("MIMI_METRICS_TOKEN", ""))
    if open_access is None:
        opened = os.environ.get("MIMI_METRICS_OPEN", "").strip() == "1"
    else:
        opened = bool(open_access)

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            return
        if scope.get("path") != path:
            await _send(send, 404, b"not found\n")
            return
        if not tok and not opened:
            await _send(send, 503, b"metrics are off: set MIMI_METRICS_TOKEN\n")
            return
        if tok and not _authorized(scope, tok):
            await _send(send, 401, b"metrics need the bearer token\n")
            return
        try:
            body = render_prometheus(snapshot(engine)).encode()
        except Exception as exc:  # never serve a half-built page
            await _send(send, 500, f"metrics unavailable: {type(exc).__name__}\n".encode())
            return
        await _send(send, 200, body,
                    content_type="text/plain; version=0.0.4; charset=utf-8")

    return app


def _authorized(scope: dict[str, Any], token: str) -> bool:
    headers = dict(scope.get("headers") or [])
    raw = headers.get(b"authorization", b"").decode("latin-1")
    value = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
    import hmac

    return hmac.compare_digest(value, token)


async def _send(send: Any, status: int, body: bytes, *,
                content_type: str = "text/plain; charset=utf-8") -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", content_type.encode()),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


def compact(snap: dict[str, Any]) -> Iterable[str]:
    """The one-line summary the CLI prints before the exposition."""
    t, q, a = snap["turns"], snap["queue"], snap["approvals"]
    yield (f"turns/min {t['per_minute_5m']} (5m) {t['per_minute_1h']} (1h); "
           f"p50 {t['latency_ms']['p50']} ms p95 {t['latency_ms']['p95']} ms")
    yield (f"queue depth {q['depth']} ({q['leased']} leased), oldest waiting "
           f"{q['oldest_wait_s']} s")
    yield (f"approvals waiting {a['waiting']}, oldest {a['oldest_wait_s']} s")
    for row in snap["spend"]:
        yield (f"spend {row['tenant_id']}: {row['turns_used']} of {row['turns_cap']} "
               f"model turns ({row['spent_kobo']} of {row['cap_kobo']} kobo, "
               f"{row['active_shops']} active shops)")


def make_server(engine: Any, *, host: str = "127.0.0.1", port: int = 9101,
                token: str = "", path: str = "/metrics") -> Any:
    """The stdlib HTTP server behind ``serve``, unstarted (a test's hook)."""
    import hmac
    import os
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    tok = str(token or os.environ.get("MIMI_METRICS_TOKEN", ""))
    opened = os.environ.get("MIMI_METRICS_OPEN", "").strip() == "1"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 (the stdlib's name)
            if self.path.split("?")[0] != path:
                self._write(404, b"not found\n")
                return
            if not tok and not opened:
                self._write(503, b"metrics are off: set MIMI_METRICS_TOKEN\n")
                return
            raw = self.headers.get("Authorization", "")
            given = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
            if tok and not hmac.compare_digest(given, tok):
                self._write(401, b"metrics need the bearer token\n")
                return
            try:
                body = render_prometheus(snapshot(engine)).encode()
            except Exception as exc:  # never serve a half-built page
                self._write(500, f"metrics unavailable: {type(exc).__name__}\n".encode())
                return
            self._write(200, body, "text/plain; version=0.0.4; charset=utf-8")

        def _write(self, status: int, body: bytes, kind: str = "text/plain") -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:  # a scraper is not a visitor
            return None

    return ThreadingHTTPServer((host, port), Handler)


def serve(engine: Any, *, host: str = "127.0.0.1", port: int = 9101,
          token: str = "", path: str = "/metrics") -> None:
    """Serve ``/metrics`` until killed: no framework, no new dependency."""
    make_server(engine, host=host, port=port, token=token, path=path).serve_forever()


def _main(argv: Sequence[str] | None = None) -> int:
    import os
    import sys

    args = list(argv or [])
    listen = ""
    if "--listen" in args:
        i = args.index("--listen")
        listen = args[i + 1] if i + 1 < len(args) else ""
        del args[i:i + 2]
    url = (args[0] if args else "") or os.environ.get("MIMI_DB_URL") \
        or os.environ.get("DATABASE_URL", "")
    if not url:
        print("usage: python -m mimi.observe.metrics [--listen HOST:PORT] <database-url>",
              file=sys.stderr)
        return 2
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    engine: Any = sa.create_engine(url)
    if listen:
        host, _, port = listen.rpartition(":")
        print(f"serving /metrics on {host or '127.0.0.1'}:{port or '9101'}", file=sys.stderr)
        serve(engine, host=host or "127.0.0.1", port=int(port or 9101))
        return 0
    snap = snapshot(engine)
    for line in compact(snap):
        print(line)
    print(render_prometheus(snap), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(_main(sys.argv[1:]))


__all__ = ["asgi_app", "compact", "make_server", "percentile", "render_prometheus",
           "serve", "snapshot"]
