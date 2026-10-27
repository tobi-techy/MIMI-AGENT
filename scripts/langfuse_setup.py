#!/usr/bin/env python3
"""Set up the Langfuse project for Mimi (``make langfuse-setup``). Idempotent.

    LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... python scripts/langfuse_setup.py

What it creates (skipping anything with the same name that already exists):

1. Model prices (``ops/langfuse/models/*.json``) so Langfuse computes cost for
   ``muse-spark-1.3`` / ``-contributor`` (tokens) and ``spitch-stt`` /
   ``muse-voice-transcribe`` (``audio_ms``).
2. A code evaluator ``reply-accusation-free`` (Python, runs in Langfuse) and an
   enabled rule on the turn roots (agent observations): a deterministic
   keyword guard on the reply Mimi proposed to the shop.
3. The LLM-as-a-judge evaluator ``reply-tone`` (same prompt as
   ``judge-calibration/reply-tone``) and its rule, created **disabled**: it
   needs an LLM connection in the project and the user's approval of the
   calibration (docs/RUN.md).
4. A dashboard "Mimi monitors" with the five monitored numbers.

Alerts themselves are created in the UI (no public API); see docs/RUN.md and
``scripts/langfuse_monitor.py`` for the scheduled checks.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOST = (os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL")
        or "https://cloud.langfuse.com").rstrip("/")
ROOTS = ["check-payment-screenshot", "handle-receipt-reply", "handle-voice-note",
         "handle-promise-reply"]
LIVE_ENVS = ["dev", "demo", "production"]

ACCUSATION_CODE = '''
import re

WORDS = re.compile(r"\\b(fake|fraud|forged|lie|lying|liar|scam|cheat|edited|steal)\\b", re.I)


def evaluate(ctx):
    out = ctx.observation.output or {}
    if isinstance(out, str):
        import json
        try:
            out = json.loads(out)
        except Exception:
            out = {}
    reply = out.get("reply_to_shop") if isinstance(out, dict) else None
    if isinstance(reply, list):
        reply = " ".join(str(r) for r in reply)
    if not reply:
        return EvaluationResult(scores=[])
    hit = WORDS.search(str(reply))
    return EvaluationResult(scores=[Score(
        name="reply_accusation_free",
        value=hit is None,
        data_type="BOOLEAN",
        comment=f"found '{hit.group(0)}'" if hit else "no accusing words",
    )])
'''.strip()


def _api(client: httpx.Client, method: str, path: str, body: Any = None) -> Any:
    r = client.request(method, f"{HOST}{path}", json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path}: {r.status_code} {r.text[:300]}")
    return r.json() if r.content else {}


def _names(rows: Any) -> dict[str, dict[str, Any]]:
    data = rows.get("data", rows) if isinstance(rows, dict) else rows
    return {str(x.get("name") or x.get("modelName")): x for x in data or []}


def models(c: httpx.Client) -> None:
    have: set[str] = set()
    page = 1
    while True:
        res = _api(c, "GET", f"/api/public/models?limit=100&page={page}")
        have |= {m["modelName"] for m in res["data"]}
        if page >= int(res.get("meta", {}).get("totalPages", 1)):
            break
        page += 1
    # Built-in models are listed too; only ours are created.
    for f in sorted((ROOT / "ops" / "langfuse" / "models").glob("*.json")):
        body = json.loads(f.read_text())
        if body["modelName"] in have:
            print(f"model {body['modelName']}: exists")
            continue
        _api(c, "POST", "/api/public/models", body)
        print(f"model {body['modelName']}: created")


def evaluators(c: httpx.Client) -> dict[str, str]:
    from mimi.observe.experiments import JUDGE_PROMPT

    have = _names(_api(c, "GET", "/api/public/v2/evaluators?limit=100"))
    ids: dict[str, str] = {}
    wanted = {
        "reply-accusation-free": {
            "type": "code", "name": "reply-accusation-free",
            "description": "Deterministic guard: the reply Mimi proposed to the shop has no "
                           "accusing words (fake, fraud, edited, ...).",
            "sourceCode": ACCUSATION_CODE, "sourceCodeLanguage": "PYTHON",
        },
        "reply-tone": {
            "type": "llm_as_judge", "name": "reply-tone",
            "description": "Never accuses the shop, no invented amounts, plain English. "
                           "Calibrated in judge-calibration/reply-tone (8/8).",
            "prompt": JUDGE_PROMPT,
            "outputDefinition": {
                "dataType": "BOOLEAN",
                "scoreValueInstructions": "true only if respectful, figures_grounded and "
                                          "plain_english are all true",
                "scoreReasoningInstructions": "one short sentence",
            },
            "variableMapping": [
                {"variable": "reply", "source": "output", "jsonPath": "$.reply_to_shop"},
                {"variable": "allowed", "source": "input"},
            ],
        },
    }
    for name, body in wanted.items():
        if name in have:
            ids[name] = str(have[name]["id"])
            print(f"evaluator {name}: exists ({ids[name]})")
            continue
        try:
            ids[name] = str(_api(c, "POST", "/api/public/v2/evaluators", body)["id"])
            print(f"evaluator {name}: created ({ids[name]})")
        except RuntimeError as exc:
            print(f"evaluator {name}: NOT created: {exc}")
    return ids


def rules(c: httpx.Client, ids: dict[str, str]) -> None:
    have = _names(_api(c, "GET", "/api/public/v2/evaluation-rules?limit=100"))
    flt = [
        {"type": "stringOptions", "column": "type", "operator": "any of", "value": ["AGENT"]},
        {"type": "stringOptions", "column": "name", "operator": "any of", "value": ROOTS},
        {"type": "stringOptions", "column": "environment", "operator": "any of",
         "value": LIVE_ENVS},
    ]
    for rule, ev, enabled in (("turn roots: reply-accusation-free", "reply-accusation-free", True),
                              ("turn roots: reply-tone (needs LLM connection)", "reply-tone",
                               False)):
        if rule in have:
            print(f"rule {rule}: exists")
            continue
        if ev not in ids:
            print(f"rule {rule}: skipped (no evaluator)")
            continue
        body = {"name": rule, "enabled": enabled, "filter": flt,
                "evaluatorAssignments": [{"evaluatorId": ids[ev]}]}
        try:
            _api(c, "POST", "/api/public/v2/evaluation-rules", body)
            print(f"rule {rule}: created ({'enabled' if enabled else 'disabled'})")
        except RuntimeError as exc:
            print(f"rule {rule}: NOT created: {exc}")


def _bool_rate(name: str, score: str, chart: str = "NUMBER") -> dict[str, Any]:
    return {"name": name, "view": "scores-boolean", "chartType": chart,
            "description": f"{score} (true rate; experiments excluded)",
            "dimensions": [], "metrics": [{"measure": "value", "agg": "sum"}],
            "filters": [{"column": "name", "operator": "any of", "value": [score],
                         "type": "stringOptions"},
                        {"column": "environment", "operator": "none of",
                         "value": ["sdk-experiment"], "type": "stringOptions"}]}


def dashboard(c: httpx.Client) -> None:
    have = _names(_api(c, "GET", "/api/public/unstable/dashboards?limit=100"))
    if "Mimi monitors" in have:
        print(f"dashboard Mimi monitors: exists ({have['Mimi monitors'].get('id')})")
        return
    gen = [{"column": "type", "operator": "any of", "value": ["GENERATION"],
            "type": "stringOptions"},
           {"column": "environment", "operator": "none of", "value": ["sdk-experiment"],
            "type": "stringOptions"}]
    widgets = [
        _bool_rate("Fakes that ended likely_genuine (critical)", "fake_ended_likely"),
        _bool_rate("Disputes that ended in consent (critical)", "dispute_ended_in_consent"),
        _bool_rate("Turn errors", "turn_error"),
        {"name": "Model latency p95 (ms)", "view": "observations", "chartType": "LINE_TIME_SERIES",
         "description": "p95 latency of Muse and speech-to-text calls",
         "dimensions": [{"field": "providedModelName"}],
         "metrics": [{"measure": "latency", "agg": "p95"}], "filters": gen},
        {"name": "Daily cost (USD)", "view": "observations", "chartType": "BAR_TIME_SERIES",
         "description": "Model cost per day, from the model prices",
         "dimensions": [{"field": "providedModelName"}],
         "metrics": [{"measure": "totalCost", "agg": "sum"}], "filters": gen},
    ]
    dash = _api(c, "POST", "/api/public/unstable/dashboards",
                {"name": "Mimi monitors",
                 "description": "Safety invariants, errors, latency and cost for Mimi's shop "
                                "turns (scripts/langfuse_setup.py)."})
    dash_id = dash.get("id")
    for i, w in enumerate(widgets):
        try:
            wid = _api(c, "POST", "/api/public/unstable/dashboard-widgets", w)["id"]
            _api(c, "POST", f"/api/public/unstable/dashboards/{dash_id}/placements",
                 {"type": "widget", "widgetId": wid, "x": (i % 2) * 6, "width": 6, "height": 5})
            print(f"widget {w['name']}: placed")
        except RuntimeError as exc:
            print(f"widget {w['name']}: NOT created: {exc}")
    print(f"dashboard Mimi monitors: {HOST}/project/~/dashboards/{dash_id}")


def main() -> int:
    pk, sk = os.environ.get("LANGFUSE_PUBLIC_KEY", ""), os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not pk or not sk:
        print("needs LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY", file=sys.stderr)
        return 2
    with httpx.Client(auth=(pk, sk), timeout=30) as c:
        models(c)
        ids = evaluators(c)
        rules(c, ids)
        dashboard(c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
