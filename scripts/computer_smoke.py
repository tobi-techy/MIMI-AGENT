"""Live smoke: one real computer task end to end on the configured browser.

This is the honest proof that the runner, the guardrails and the browser
container work together for real, rather than only against the offline fake.

Put the key in the environment first, never on the command line:

    set -a; . ~/.mimi/browserbase.env; set +a
    /opt/homebrew/bin/python3.11 scripts/computer_smoke.py \
        --url https://books.toscrape.com/ --domain books.toscrape.com

It builds the plan in code for the task's kind, runs it on a real browser
session, writes every step and every screenshot artifact to a temporary store,
and prints what happened. It also reads the session's live view link while the
session is still open, and fetches that same link with no provider credentials
at all, so the report can say plainly whether a person who is not logged in to
the provider can open it.

The key is never printed. The signed live view token is redacted in the report.
The session is released before the script exits.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from mimi.computer import plans  # noqa: E402
from mimi.computer.cloud_browser import CloudBrowserEngine, CloudConfig  # noqa: E402
from mimi.computer.runner import Outcome, Runner  # noqa: E402
from mimi.computer.store import ComputerStore, plan_dicts  # noqa: E402
from mimi.computer.types import GuardLimits  # noqa: E402
from mimi.store.db import ensure_schema, make_engine  # noqa: E402

TENANT = "smoke"


class Watched:
    """The real engine, plus the live view link read while the session is open."""

    def __init__(self, inner: CloudBrowserEngine) -> None:
        self.inner = inner
        self.name = inner.name
        self.live = ""

    def start(self, task: Any) -> None:
        self.inner.start(task)
        self.live = self.inner.sign_in_link(task)

    def do(self, task: Any, action: str, **kw: Any) -> Any:
        page = self.inner.do(task, action, **kw)
        if not self.live:
            self.live = self.inner.sign_in_link(task)
        return page

    def sign_in_link(self, task: Any) -> str:
        return self.inner.sign_in_link(task)

    def stop(self, task: Any, reason: str) -> None:
        self.inner.stop(task, reason)


def redact_link(url: str) -> str:
    """The link's shape, with the signed token itself never written out."""
    import re

    return re.sub(r"([?&][^=]*(?:t|token|key|sig)[^=]*=)[^&\s]+", r"\1<signed>", url)


def check_link(url: str) -> dict[str, Any]:
    """Fetch the live view page with no provider credentials at all."""
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
    task_id = f"ct_smoke_{uuid.uuid4().hex[:10]}"
    plan = plans.build(args.kind, args.goal, args.url, args.domain)
    engine = Watched(CloudBrowserEngine(CloudConfig(key=key, base=base)))

    db = make_engine("sqlite://")
    ensure_schema(db)
    store = ComputerStore(db, artifact_dir=workdir)
    store.create(TENANT, task_id, args.goal, args.url, args.domain, plan.kind,
                 plan_dicts(plan.steps), {}, GuardLimits().model_dump(), engine.name)

    def on_step(record: Any, blob: Any) -> None:
        store.add_step(TENANT, task_id, record)
        if blob is not None:
            store.add_artifact(TENANT, task_id, blob)

    def on_artifact(blob: Any) -> None:
        store.add_artifact(TENANT, task_id, blob)

    runner = Runner(engine, limits=GuardLimits(), on_step=on_step,
                    on_artifact=on_artifact)
    outcome = Outcome()
    try:
        outcome = runner.run(task_id, args.domain, plan.kind, plan.steps, {})
    finally:
        link = engine.live
        if not link:
            link = engine.sign_in_link(type("R", (), {"task_id": task_id})())
        report = {
            "task_id": task_id,
            "kind": plan.kind,
            "url": args.url,
            "domain": args.domain,
            "engine": engine.name,
            "status": outcome.status,
            "stop_reason": outcome.stop_reason,
            "note": outcome.note,
            "steps": [{"index": s.index, "action": s.action, "url": s.url,
                       "note": s.note, "screenshot": s.screenshot}
                      for s in outcome.steps],
            "artifacts": [{"name": a.name, "kind": a.kind, "size": len(a.data),
                           "path": str(store.dir / TENANT / task_id / a.name)}
                          for a in outcome.artifacts],
            "artifacts_on_disk": sorted(
                p.name for p in (store.dir / TENANT / task_id).glob("*"))
            if (store.dir / TENANT / task_id).is_dir() else [],
            "live_view": redact_link(link),
            "live_view_reachable_without_login": check_link(link),
            "workdir": workdir,
        }
        print(json.dumps(report, indent=2))
    return 0 if outcome.status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
