"""Live smoke: one real computer task end to end on the configured browser.

This is the honest proof that the service, the runner, the guardrails and the
browser container work together for real, rather than only against the offline
fake. It goes through the same service the API uses, so it also proves the
console contract: the step log as it fills, the artifacts on disk, and the
signed ``live_view_url`` that the console opens in a new tab.

Put the key in the environment first, never on the command line:

    set -a; . ~/.mimi/browserbase.env; set +a
    MIMI_CLOUD_BROWSER_API_KEY="$BROWSERBASE_API_KEY" \
    /opt/homebrew/bin/python3.11 scripts/computer_smoke.py \
        --url https://books.toscrape.com/ --domain books.toscrape.com

The key is never printed. The signed live view token is redacted. The session
is released before the script exits.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from mimi.computer.cloud_browser import CloudBrowserEngine, CloudConfig  # noqa: E402
from mimi.computer.service import ComputerService  # noqa: E402
from mimi.computer.store import ComputerStore  # noqa: E402
from mimi.store.db import ensure_schema, make_engine  # noqa: E402

TENANT = "smoke"
DEADLINE_S = 300.0


def redact_link(url: str) -> str:
    """The link's shape, with the signed token itself never written out."""
    return re.sub(r"([?&][^=]*(?:t|token|key|sig)[^=]*=)[^&\s]+", r"\1<signed>", url)


def check_link(url: str) -> dict[str, Any]:
    """Open the live view page with no provider credentials at all."""
    if not url:
        return {"ok": False, "reason": "no live view link"}
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
    except httpx.HTTPError as exc:
        return {"ok": False, "reason": str(exc)[:200]}
    body = r.text[:20000].lower()
    return {"ok": r.status_code < 400, "status": r.status_code,
            "content_type": r.headers.get("content-type", ""),
            "is_devtools": "inspector" in body or "devtools" in body,
            "bytes": len(r.content)}


def main() -> int:
    ap = argparse.ArgumentParser(description="live computer-task smoke")
    ap.add_argument("--url", default="https://books.toscrape.com/")
    ap.add_argument("--domain", default="books.toscrape.com")
    ap.add_argument("--kind", default="price_check")
    ap.add_argument("--goal", default="Check this page for the prices it shows")
    ap.add_argument("--dir", default="")
    args = ap.parse_args()

    key = os.environ.get("MIMI_CLOUD_BROWSER_API_KEY", "").strip()
    if not key:
        print("no MIMI_CLOUD_BROWSER_API_KEY in the environment", file=sys.stderr)
        return 2
    base = (os.environ.get("MIMI_CLOUD_BROWSER_BASE", "").strip()
            or "https://api.browserbase.com")

    workdir = args.dir or tempfile.mkdtemp(prefix="mimi-computer-smoke-")
    Path(workdir).mkdir(parents=True, exist_ok=True)
    # A file-backed store, like a deployment: the run happens on a background
    # thread while this script polls, and an in-memory SQLite hands both threads
    # the same connection, which is not what a real deployment uses.
    db = make_engine(f"sqlite:///{workdir}/smoke.sqlite3")
    ensure_schema(db)
    store = ComputerStore(db, artifact_dir=workdir)
    service = ComputerService(store, CloudBrowserEngine(CloudConfig(key=key, base=base)))

    proposed = service.propose(TENANT, args.goal, args.url, args.domain, args.kind)
    task_id = proposed["task_id"]
    service.approve(TENANT, task_id)

    seen_live: list[str] = []
    seen_steps: list[int] = []
    view: dict[str, Any] = {}
    started = time.time()
    while time.time() - started < DEADLINE_S:
        view = service.get(TENANT, task_id) or {}
        if view.get("live_view_url"):
            seen_live.append(view["live_view_url"])
        seen_steps.append(len(view.get("steps", [])))
        if view.get("status") not in ("running", "proposed"):
            break
        time.sleep(0.5)

    steps = view.get("steps", [])
    artifacts = view.get("artifacts", [])
    task_dir = store.dir / TENANT / task_id
    report = {
        "task_id": task_id,
        "kind": view.get("kind", args.kind),
        "url": args.url,
        "domain": args.domain,
        "engine": service.engine.name,
        "plan": [{"action": s["action"], "code": s["code"]} for s in proposed["plan"]],
        "status": view.get("status"),
        "stop_reason": view.get("stop_reason", ""),
        "note": view.get("note", ""),
        "steps": [{"index": s["index"], "action": s["action"], "code": s["code"],
                   "url": s["url"],
                   "note": s["note"], "screenshot": s["screenshot"]} for s in steps],
        "step_count_seen_while_running": {"max": max(seen_steps or [0]),
                                          "last": seen_steps[-6:],
                                          "polls": len(seen_steps)},
        "artifacts": [{"name": a["name"], "kind": a["kind"], "mime": a["mime"],
                       "size": a["size"], "sha256": a["sha256"]} for a in artifacts],
        "artifacts_on_disk": sorted(p.name for p in task_dir.glob("*"))
        if task_dir.is_dir() else [],
        "live_view_url_seen_while_running": redact_link(seen_live[0]) if seen_live else None,
        "live_view_url_after_the_run": view.get("live_view_url"),
        "live_view_reachable_without_login": check_link(seen_live[0] if seen_live else ""),
        "workdir": workdir,
        "seconds": round(time.time() - started, 1),
    }
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
