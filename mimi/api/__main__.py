"""``python -m mimi.api {serve,worker,openapi}``

* ``serve``: the HTTP API with ``MIMI_WORKERS`` worker threads inside it.
* ``worker``: a worker process only (scale these beside ``serve`` with
  ``MIMI_WORKERS=0``). Safe to kill at any time: its turns are taken over.
* ``openapi``: write the OpenAPI document (``docs/api/openapi.json``).
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.api")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="run the HTTP API")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    wp = sub.add_parser("worker", help="run a worker process")
    wp.add_argument("--db", default="", help="SQLAlchemy URL (default: DATABASE_URL)")
    wp.add_argument("--idle", type=float, default=0.5, help="seconds between empty polls")
    op = sub.add_parser("openapi", help="write the OpenAPI document")
    op.add_argument("--out", default="docs/api/openapi.json")
    op.add_argument("--check", action="store_true", help="exit 1 if the file is stale")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("mimi.api.app:create_app", factory=True, host=args.host, port=args.port)
        return 0

    if args.cmd == "worker":
        from mimi.api.agent import Agent
        from mimi.api.config import Settings
        from mimi.store.db import make_engine

        settings = Settings.from_env()
        settings.check()
        agent = Agent.from_env(make_engine(args.db or settings.database_url),
                               lease_s=settings.lease_s)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        logging.info("worker %s up (model %s)", agent.worker_id, agent.model_mode)
        agent.run_worker(stop, idle_s=args.idle)
        return 0

    from mimi.api.app import openapi_json

    text = json.dumps(openapi_json(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    out = Path(args.out)
    if args.check:
        if not out.exists() or out.read_text(encoding="utf-8") != text:
            print(f"{out} is stale: run python -m mimi.api openapi", file=sys.stderr)
            return 1
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
