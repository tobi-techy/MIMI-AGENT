"""The live week's spend meter: every call to Meta's API, priced, on disk.

The harness records its own model spend in the store (``spend``), but the
router, the helpers' readers (vision, receipt judge, stock answers) and SAM
3.1 call the API on their own clients. A live week's naira ceiling has to
see all of it, so a live worker wraps ``httpx.Client.send`` and, for every
response from the Meta host, appends one line to ``meter-<worker>.jsonl``:
path, model, status, tokens, kobo. Prices: ``TOKEN_PRICES`` for chat
(an unknown model name is priced as Muse Spark 1.3, the dearest), SAM 3.1 at
``SAM_USD_PER_IMAGE`` per image.

Before every call the meter adds up every worker's file; at or over the
ceiling (``MIMI_WEEK_MAX_KOBO``) it refuses the call, so the ceiling holds
even mid-wave (overshoot is at most the calls already in flight). A 401 or
403 writes ``auth-failed`` in the workdir; the runner stops on it.
The key is never read or written here.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from mimi.credit.sam import sam_cost_kobo
from mimi.harness.gateway import kobo_cost
from mimi.observe.turns import TOKEN_PRICES

HOST = "api.meta.ai"
_lock = threading.Lock()


def total_kobo(workdir: Path) -> int:
    n = 0
    for f in workdir.glob("meter-*.jsonl"):
        for line in f.read_text().splitlines():
            try:
                n += int(json.loads(line).get("kobo") or 0)
            except ValueError:
                continue
    return n


def summary(workdir: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for f in sorted(workdir.glob("meter-*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    by: dict[str, dict[str, Any]] = {}
    for r in rows:
        k = f"{r.get('path')} {r.get('model')}"
        b = by.setdefault(k, {"calls": 0, "kobo": 0, "prompt_tokens": 0,
                              "completion_tokens": 0, "statuses": {}})
        b["calls"] += 1
        b["kobo"] += int(r.get("kobo") or 0)
        b["prompt_tokens"] += int(r.get("pt") or 0)
        b["completion_tokens"] += int(r.get("ct") or 0)
        st = str(r.get("status"))
        b["statuses"][st] = b["statuses"].get(st, 0) + 1
    lat = sorted(float(r.get("s") or 0) for r in rows)
    return {"calls": len(rows), "kobo": sum(int(r.get("kobo") or 0) for r in rows),
            "by_endpoint": by, "refused_at_ceiling": sum(1 for r in rows if r.get("refused")),
            "call_latency_p50_s": lat[len(lat) // 2] if lat else 0.0,
            "call_latency_max_s": lat[-1] if lat else 0.0}


def price(path: str, req: dict[str, Any], data: dict[str, Any]) -> tuple[int, str, int, int]:
    model = str(req.get("model") or data.get("model") or "")
    if path.endswith("/responses") or model.startswith("sam"):
        images = 0
        for item in req.get("input") or []:
            for part in (item.get("content") or []) if isinstance(item, dict) else []:
                if isinstance(part, dict) and part.get("type") == "input_image":
                    images += 1
        return sam_cost_kobo(max(1, images)), model or "sam-3.1", 0, 0
    usage = data.get("usage") or {}
    pt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    ct = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    priced = model if model in TOKEN_PRICES else "muse-spark-1.3"
    return kobo_cost(priced, pt, ct), model, pt, ct


def install(workdir: Path, worker_id: str) -> None:
    ceiling = int(os.environ.get("MIMI_WEEK_MAX_KOBO", "0") or 0)
    out = workdir / f"meter-{worker_id}.jsonl"
    orig = httpx.Client.send

    def send(self: httpx.Client, request: httpx.Request, *a: Any, **kw: Any) -> httpx.Response:
        if request.url.host != HOST:
            return orig(self, request, *a, **kw)
        path = request.url.path
        if ceiling and total_kobo(workdir) >= ceiling:
            with _lock, out.open("a") as f:
                f.write(json.dumps({"path": path, "refused": True, "kobo": 0}) + "\n")
            raise httpx.TransportError("week naira ceiling reached: call refused")
        t0 = time.monotonic()
        resp = orig(self, request, *a, **kw)
        secs = time.monotonic() - t0
        try:
            req = json.loads(request.content or b"{}")
        except ValueError:
            req = {}
        try:
            data = resp.json() if resp.status_code == 200 else {}
        except ValueError:
            data = {}
        kobo, model, pt, ct = price(path, req, data) if resp.status_code == 200 \
            else (0, str(req.get("model") or ""), 0, 0)
        if resp.status_code in (401, 403):
            (workdir / "auth-failed").write_text(f"{resp.status_code} {path}\n")
        with _lock, out.open("a") as f:
            f.write(json.dumps({"path": path, "model": model, "status": resp.status_code,
                                "pt": pt, "ct": ct, "kobo": kobo, "s": round(secs, 3),
                                "at": time.time()}) + "\n")
        return resp

    httpx.Client.send = send  # type: ignore[method-assign]
