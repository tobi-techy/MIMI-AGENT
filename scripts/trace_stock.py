#!/usr/bin/env python3
"""Low-stock scene traced to Langfuse (``make trace-stock``).

    LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... python scripts/trace_stock.py --public

Runs ``python -m mimi.sim --stock`` (shop-007, shop-004, shop-002 by default)
and ``--stock-words`` (shops type, so Muse Spark reads every answer),
with every stock turn exported to Langfuse (environment ``demo``): the
morning check that asked (``check-shop-stock``: the estimator's inputs and
outputs) and the shop's answer (``handle-stock-answer``: the read, the
re-estimate, the draft verifier and what Mimi said). The model answers are
recorded in ``.cache/trace-stock.*.json`` and replayed on a rerun. With
``--public`` the first answer trace that ended in a restock draft is made
public and its link printed (for judges).

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

SHOPS = "shop-007,shop-004,shop-002"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--public", action="store_true", help="make the first draft trace public")
    ap.add_argument("--shop-ids", default=SHOPS)
    ap.add_argument("--cache", default=str(ROOT / ".cache" / "trace-stock"))
    args = ap.parse_args(argv)
    settings = LangfuseSettings.from_env(environment=os.environ.get(
        "LANGFUSE_TRACING_ENVIRONMENT", "demo"))
    if settings is None:
        print("trace-stock needs LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY (and not CI)",
              file=sys.stderr)
        return 2
    picked: list[str] = []

    def public_if(turn: Turn) -> bool:
        if args.public and not picked and turn.flow == "stock_answer" \
                and turn.metadata.get("action") == "draft":
            picked.append(turn.job_id)
            return True
        return False

    exporter = LangfuseExporter(settings, public_if=public_if)
    Path(args.cache).parent.mkdir(parents=True, exist_ok=True)
    out = io.StringIO()
    with redirect_stdout(out):
        code = sim_main(["--stock", "--stock-words", "--shop-ids", args.shop_ids, "--transcript",
                         "--cache", args.cache], exporter=exporter)
    text = out.getvalue()
    report = json.loads(text[text.index("\n{") + 1:]) if "\n{" in text else {}
    print(text[: text.index("\n{")] if "\n{" in text else text)
    rows = [{"trace": t.name, "verdict": t.verdict, "tags": t.tags, "public": t.public,
             "url": exporter.trace_url(t.trace_id)} for t in exporter.sent]
    print(json.dumps({
        "environment": settings.environment,
        "release": settings.release,
        "traces": rows,
        "scores_sent": exporter.scores_sent,
        "export_errors": exporter.errors,
        "dropped": exporter.dropped,
        "mimi_model": report.get("mimi_model"),
        "restock_drafts": report.get("restock_drafts"),
        "live_calls_this_pass": report.get("live_calls_this_pass"),
        "replayed_calls": report.get("replayed_calls"),
    }, indent=1))
    return code if not exporter.errors else 1


if __name__ == "__main__":
    sys.exit(main())
