#!/usr/bin/env python3
"""Three-shop smoke run with Langfuse tracing on (``make trace-smoke``).

    LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... python scripts/trace_smoke.py

Runs the payment-screenshot sim for shop-019, shop-037 and shop-025 (one
genuine screenshot, one fake that the checks catch, one resend), with every
shop turn exported to Langfuse (environment ``demo``). The model answers are
recorded in ``.cache/trace-smoke.*.json`` and replayed on a rerun, so a second
smoke costs no Muse calls. With ``--public`` the trace of the first fake that
was caught is made public, and its link is printed (for judges).

Without the Langfuse keys it exits 2 and sends nothing. In CI it refuses too.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mimi.observe.langfuse_export import LangfuseExporter, LangfuseSettings  # noqa: E402
from mimi.observe.turns import Turn  # noqa: E402
from mimi.sim.__main__ import main as sim_main  # noqa: E402

SHOPS = "shop-019,shop-037,shop-025"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--public", action="store_true", help="make the first caught fake public")
    ap.add_argument("--cache", default=str(ROOT / ".cache" / "trace-smoke"))
    args = ap.parse_args(argv)
    settings = LangfuseSettings.from_env(environment=os.environ.get(
        "LANGFUSE_TRACING_ENVIRONMENT", "demo"))
    if settings is None:
        print("trace-smoke needs LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY (and not CI)",
              file=sys.stderr)
        return 2
    picked: list[str] = []

    def public_if(turn: Turn) -> bool:
        if args.public and not picked and turn.flow == "payment_screenshot" \
                and turn.verdict == "looks_fake":
            picked.append(turn.job_id)
            return True
        return False

    exporter = LangfuseExporter(settings, public_if=public_if)
    Path(args.cache).parent.mkdir(parents=True, exist_ok=True)
    out = io.StringIO()
    with redirect_stdout(out):
        code = sim_main(
            ["--shop-ids", SHOPS, "--transcript", "--screenshots", "1", "--fakes", "0.4",
             "--cache", args.cache],
            exporter=exporter,
        )
    text = out.getvalue()
    report = json.loads(text[text.index("\n{") + 1:]) if "\n{" in text else {}
    print(text[: text.index("\n{")] if "\n{" in text else text)
    rows = []
    for t in exporter.sent:
        rows.append({"trace": t.name, "verdict": t.verdict, "tags": t.tags,
                     "public": t.public, "url": exporter.trace_url(t.trace_id)})
    print(json.dumps({
        "environment": settings.environment,
        "release": settings.release,
        "traces": rows,
        "scores_sent": exporter.scores_sent,
        "export_errors": exporter.errors,
        "dropped": exporter.dropped,
        "mimi_model": report.get("mimi_model"),
        "live_calls_this_pass": report.get("live_calls_this_pass"),
        "replayed_calls": report.get("replayed_calls"),
    }, indent=1))
    return code if not exporter.errors else 1


if __name__ == "__main__":
    sys.exit(main())
