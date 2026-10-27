#!/usr/bin/env python3
"""Scheduled monitor checks against Langfuse (``make langfuse-monitor``).

    LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... python scripts/langfuse_monitor.py [--hours 24]

Langfuse Cloud alerts are created in the UI (no public API yet) and the
Hobby plan allows two per organisation, so the two critical ones are set up
there (docs/RUN.md) and this script checks all five from the Metrics API v2
(``GET /api/public/v2/metrics``). It prints one line per check and exits 1
when any check breaches, so a cron job or the disabled-by-default
``.github/workflows/langfuse-monitor.yml`` can page on it.

Checks (window: the last ``--hours``; experiments are excluded):

* CRITICAL fake_ended_likely: any ``fake_ended_likely`` score true (a fake
  screenshot ended ``likely_genuine``; sim and demo runs know the truth);
* CRITICAL bank_rejected_likely: any ``bank_confirmed`` score false (a
  "likely" payment the bank feed never saw: the production ground truth);
* CRITICAL dispute_ended_in_consent: any ``dispute_ended_in_consent`` true;
* error rate: share of ``turn_error`` true above ``--max-error-rate`` (5%);
* latency p95: p95 latency of generations above ``--max-p95-ms`` (20,000);
* daily cost: total cost of generations above ``--max-cost-usd`` (USD 2).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

HOST = (os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL")
        or "https://cloud.langfuse.com").rstrip("/")
NOT_EXPERIMENT = {"column": "environment", "operator": "none of", "value": ["sdk-experiment"],
                  "type": "stringOptions"}


def metrics(client: httpx.Client, query: dict[str, Any]) -> list[dict[str, Any]]:
    r = client.get(f"{HOST}/api/public/v2/metrics", params={"query": json.dumps(query)})
    r.raise_for_status()
    return list(r.json().get("data", []))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--max-error-rate", type=float, default=0.05)
    ap.add_argument("--max-p95-ms", type=float, default=20_000)
    ap.add_argument("--max-cost-usd", type=float, default=2.0)
    args = ap.parse_args(argv)
    pk, sk = os.environ.get("LANGFUSE_PUBLIC_KEY", ""), os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not pk or not sk:
        print("needs LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY", file=sys.stderr)
        return 2
    now = datetime.now(UTC)
    window = {"fromTimestamp": (now - timedelta(hours=args.hours)).isoformat(),
              "toTimestamp": now.isoformat()}
    breaches = 0
    with httpx.Client(auth=(pk, sk), timeout=20) as client:
        scores = {
            row["name"]: row
            for row in metrics(client, {
                "view": "scores-boolean",
                "metrics": [{"measure": "value", "aggregation": "sum"},
                            {"measure": "count", "aggregation": "count"}],
                "dimensions": [{"field": "name"}],
                "filters": [NOT_EXPERIMENT], **window})
        }

        def true_count(name: str) -> tuple[int, int]:
            row = scores.get(name) or {}
            return int(row.get("sum_value") or 0), int(row.get("count_count") or 0)

        def report(ok: bool, label: str, detail: str) -> None:
            nonlocal breaches
            breaches += 0 if ok else 1
            print(f"{'ok  ' if ok else 'FAIL'} {label}: {detail}")

        n, of = true_count("fake_ended_likely")
        report(n == 0, "CRITICAL fake_ended_likely", f"{n} of {of} screenshot turns")
        yes, of = true_count("bank_confirmed")
        report(of - yes == 0, "CRITICAL bank_rejected_likely",
               f"{of - yes} of {of} likely payments not seen by the bank")
        n, of = true_count("dispute_ended_in_consent")
        report(n == 0, "CRITICAL dispute_ended_in_consent", f"{n} of {of} receipt turns")
        n, of = true_count("turn_error")
        rate = n / of if of else 0.0
        report(rate <= args.max_error_rate, "error rate",
               f"{rate:.1%} of {of} turns (max {args.max_error_rate:.0%})")
        gen = metrics(client, {
            "view": "observations",
            "metrics": [{"measure": "latency", "aggregation": "p95"},
                        {"measure": "totalCost", "aggregation": "sum"},
                        {"measure": "count", "aggregation": "count"}],
            "dimensions": [],
            "filters": [{"column": "type", "operator": "=", "value": "GENERATION",
                         "type": "string"}, NOT_EXPERIMENT],
            **window})
        row = gen[0] if gen else {}
        p95 = float(row.get("p95_latency") or 0)
        cost = float(row.get("sum_totalCost") or 0)
        report(p95 <= args.max_p95_ms, "latency p95",
               f"{p95:,.0f} ms over {int(row.get('count_count') or 0)} model calls "
               f"(max {args.max_p95_ms:,.0f})")
        per_day = cost * 24 / args.hours
        report(per_day <= args.max_cost_usd, "daily cost",
               f"USD {per_day:.4f} per day (max {args.max_cost_usd:.2f})")
    return 1 if breaches else 0


if __name__ == "__main__":
    sys.exit(main())
