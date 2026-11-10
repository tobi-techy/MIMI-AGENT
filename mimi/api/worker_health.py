"""The worker's health endpoints (Phase 0, 10 Nov 2026).

A worker process has no HTTP API of its own, but a platform probe still needs
one question answered: can this process claim work right now? ``/healthz``
asks the queue itself (a database round trip and the claim's own bounded
read), so a worker whose database connection is gone reports 503 instead of
looking alive. ``/livez`` is the process only, for a liveness probe.

``draining`` is true from SIGTERM until the current turn finishes: readiness
goes 503 at once (no new traffic or work is sent here) while the process keeps
running the turn it holds, so a rolling deploy never loses a claim.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from mimi.clock import wall_utc


def health_payload(agent: Any, draining: bool) -> tuple[int, dict[str, Any]]:
    """(status code, body) for ``/healthz``; the queue is the truth."""
    can_claim = bool(agent.queue.probe())
    stats: dict[str, Any] = {}
    try:
        stats = agent.queue.stats()
    except Exception:
        stats = {}
    ok = can_claim and not draining
    return (200 if ok else 503), {
        "status": "ok" if ok else "degraded",
        "worker_id": str(getattr(agent, "worker_id", "")),
        "draining": bool(draining),
        "can_claim": can_claim,
        "queue_depth": int(stats.get("depth", 0) or 0),
        "dead_letters": int(stats.get("failed", 0) or 0),
        "checked_at": wall_utc().isoformat(),
    }


class WorkerHealth:
    """A stdlib HTTP server in a daemon thread; no new dependency."""

    def __init__(self, agent: Any, draining: threading.Event, *,
                 host: str = "0.0.0.0", port: int = 0) -> None:
        self.agent = agent
        self.draining = draining
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        """Start serving; returns the actual port (useful when ``port`` is 0)."""
        if self._server is not None:
            return self.port
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib's name
                if self.path.split("?", 1)[0] == "/livez":
                    outer._send(self, 200, {"status": "ok"})
                elif self.path.split("?", 1)[0] == "/healthz":
                    code, body = health_payload(outer.agent, outer.draining.is_set())
                    outer._send(self, code, body)
                else:
                    outer._send(self, 404, {"status": "not found"})

            def log_message(self, *_args: Any) -> None:
                return  # the worker logs its own lines; probes are not noise

        server = ThreadingHTTPServer((self.host, self.port), Handler)
        server.daemon_threads = True
        self._server = server
        self.port = int(server.server_address[1])
        self._thread = threading.Thread(target=server.serve_forever, daemon=True,
                                        name="mimi-worker-health")
        self._thread.start()
        return self.port

    @staticmethod
    def _send(handler: BaseHTTPRequestHandler, code: int, body: dict[str, Any]) -> None:
        data = (json.dumps(body, sort_keys=True) + "\n").encode()
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(data)

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
