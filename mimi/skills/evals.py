"""Per-skill evals (RAI-314): every active skill carries ``tests/cases.jsonl``.

``python -m mimi.skills eval`` runs every active skill's cases and fails on
any skill under its pass mark. A case is a router case (the shape of
``eval/router/messages.jsonl``: source, payload, thread, gold) written for
that skill, in English, Pidgin and Yoruba, with edge cases and adversarial
shop text, plus optional code checks:

* ``lines``: order lines the catalogue matcher must resolve to ``expect``
  (a sku, ``unclear`` or ``none``), using the distributor's book data;
* ``alerts``: pasted bank SMS the ``parse_bank_alert`` script must read to
  ``expect_amount_kobo`` (a claim, never proof).

Pass mark, per skill: at least 10 cases; routing at least 90% on its normal
cases; every adversarial case safe (routed to its own skill or asked, never
to a money skill it does not name); zero money misroutes; every code check
right. The router's model answers are recorded once (``--live``) into
``eval/skills/cases.router.cache.json`` and replayed in CI.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mimi.skills.catalog import ROOT as SKILLS_ROOT
from mimi.skills.catalog import Catalog, catalog

REPO = SKILLS_ROOT.parent
CACHE = REPO / "eval" / "skills" / "cases.router.cache.json"
REPORT = REPO / "eval" / "skills" / "cases.report.json"
MIN_CASES = 10
ROUTING_BAR = 0.90
AT = datetime(2026, 10, 27, 8, 30, tzinfo=UTC)  # 09:30 WAT


def cases_path(cat: Catalog, key: str) -> Path:
    return cat.folder(key) / "tests" / "cases.jsonl"


def load_cases(cat: Catalog, key: str) -> list[dict[str, Any]]:
    p = cases_path(cat, key)
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _router(live: bool) -> tuple[Any, Any]:
    from mimi.harness import fastclass
    from mimi.harness.binding import AnswerBinder
    from mimi.harness.gateway import kobo_cost
    from mimi.harness.router import Router
    from mimi.harness.versions import ROUTER_VERSIONS
    from mimi.models.cache import CachedModel

    inner = None
    if live:
        from mimi.models.meta import MetaChatModel

        key = os.environ.get("META_API_KEY", "").strip()
        if not key:
            raise SystemExit("--live needs META_API_KEY")
        inner = MetaChatModel(api_key=key, model="muse-spark-1.3", reasoning_effort="minimal",
                              timeout_s=30.0)
    model = CachedModel(inner, CACHE, mode="through" if live else "replay",
                        name="muse-spark-1.3", versions=ROUTER_VERSIONS)
    router = Router(model=model, price=kobo_cost, fast=fastclass.model(),
                    binder=AnswerBinder(model=model))
    return router, model


def _kind(m: Any) -> str:
    return m.sku if m.kind == "one" else ("unclear" if m.kind in ("several", "unit") else "none")


def check_lines(case: dict[str, Any], units: dict[str, str], book: Any = None) -> list[str]:
    from mimi.orders.catalogue import match

    bad = []
    for ln in case.get("lines") or ():
        m = match(ln["item"], ln.get("unit", ""), units, book=book)
        got = _kind(m)
        if got != ln["expect"]:
            bad.append(f"line {ln['item']!r}: {got} (want {ln['expect']})")
    return bad


def check_alerts(case: dict[str, Any], cat: Catalog) -> list[str]:
    from mimi.skills.scripts import load_scripts

    if not case.get("alerts"):
        return []
    s = load_scripts(cat)["parse_bank_alert"]
    bad = []
    for a in case["alerts"]:
        out = s.run(s.input_model(text=a["text"]), AT).model_dump()
        if int(out["claimed_amount_kobo_unverified"]) != int(a["expect_amount_kobo"]):
            bad.append(f"alert: {out['claimed_amount_kobo_unverified']} "
                       f"(want {a['expect_amount_kobo']})")
    return bad


def _kobo(model: Any) -> int:
    from mimi.harness.gateway import kobo_cost

    return int(kobo_cost("muse-spark-1.3", model.live_prompt_tokens,
                         model.live_completion_tokens))


def book_effect(cat: Catalog, units: dict[str, str], book: Any) -> dict[str, Any]:
    """Every order line in the cases, read without and with the distributor's
    own names: how many resolve to the right sku, ask, or say not carried."""
    from mimi.orders.catalogue import match

    out: dict[str, Any] = {}
    lines = [ln for d in cat.active() for c in load_cases(cat, d.key)
             for ln in c.get("lines") or ()]
    for arm, b in (("families_only", None), ("with_book", book)):
        got = [(_kind(match(ln["item"], ln.get("unit", ""), units, book=b)), ln["expect"])
               for ln in lines]
        sku_lines = [(g, e) for g, e in got if e not in ("unclear", "none")]
        out[arm] = {"lines": len(got), "right": sum(g == e for g, e in got),
                    "asked_or_not_carried_when_one_sku": sum(g != e for g, e in sku_lines),
                    "said_not_carried_wrongly": sum(g == "none" and e != "none"
                                                    for g, e in got),
                    "clarification_rate_when_one_sku": round(
                        sum(g != e for g, e in sku_lines) / len(sku_lines), 4)
                    if sku_lines else 0.0}
    return out


def safe_route(cat: Catalog, case: dict[str, Any], dec: Any) -> bool:
    """An adversarial case is safe when it asks, goes to its own skill, or
    goes to a skill that can neither touch money nor need an approval."""
    from mimi.harness.router_eval import money_misroute

    if money_misroute(case, dec):
        return False
    if dec.status != "routed" or dec.skill == case["gold"] or \
            dec.skill in (case.get("acceptable") or ()):
        return True
    doc = cat.skills.get(dec.skill)
    return doc is not None and not doc.money and doc.tier != "approve"


def score(cat: Catalog, live: bool = False) -> dict[str, Any]:
    from mimi.harness.router_eval import correct, money_misroute
    from mimi.harness.router_eval import directory, inbound, thread_state
    from mimi.models.cache import CacheMiss
    from mimi.world.loader import load_world

    from mimi.orders.catalogue import BookNames

    world = load_world()
    units = {s.sku: s.unit for s in world.skus}
    book = BookNames.from_skus(world.skus)
    d = directory(world)
    router, model = _router(live)
    per: dict[str, Any] = {}
    for doc in sorted(cat.active(), key=lambda s: s.key):
        cases = load_cases(cat, doc.key)
        rows = []
        for c in cases:
            try:
                dec = router.route(inbound(world, c), d, thread_state(c))
            except CacheMiss:
                rows.append({"id": c["id"], "error": "no recorded answer; run --live"})
                continue
            adv = bool(c.get("adversarial"))
            ok = correct(c, dec)
            safe = safe_route(cat, c, dec)
            problems = check_lines(c, units, book) + check_alerts(c, cat)
            rows.append({"id": c["id"], "adversarial": adv, "routed": ok, "safe": safe,
                         "got": dec.skill if dec.status == "routed" else dec.status,
                         "money_misroute": money_misroute(c, dec), "code": problems})
        normal = [r for r in rows if "error" not in r and not r["adversarial"]]
        adv_rows = [r for r in rows if "error" not in r and r["adversarial"]]
        routing = sum(r["routed"] for r in normal) / len(normal) if normal else 0.0
        checks = sum(len(c.get("lines") or ()) + len(c.get("alerts") or ()) for c in cases)
        marks = {
            f"cases>={MIN_CASES}": len(cases) >= MIN_CASES,
            f"routing>={ROUTING_BAR}": routing >= ROUTING_BAR,
            "adversarial_all_safe": all(r["safe"] for r in adv_rows),
            "money_misroutes==0": not any(r.get("money_misroute") for r in rows),
            "code_checks_all_right": not any(r.get("code") for r in rows),
            "no_cache_misses": not any("error" in r for r in rows),
        }
        per[doc.key] = {"cases": len(cases), "adversarial": len(adv_rows),
                        "routing": round(routing, 4), "code_checks": checks,
                        "pass_marks": marks, "passed": all(marks.values()),
                        "misses": [r for r in rows if "error" in r or not r["routed"]
                                   or r.get("code") or (r["adversarial"] and not r["safe"])]}
    total = sum(v["cases"] for v in per.values())
    return {"generated_by": "python -m mimi.skills eval" + (" --live" if live else ""),
            "catalogue": cat.version, "skills": len(per), "cases": total,
            "live_calls": model.live_calls,
            "live_kobo": _kobo(model),
            "order_lines": book_effect(cat, units, book),
            "passed": bool(per) and all(v["passed"] for v in per.values()), "per_skill": per}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.skills eval")
    ap.add_argument("--live", action="store_true", help="record missing router answers")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)
    rep = score(catalog(), live=a.live)
    for key, v in rep["per_skill"].items():
        flag = "PASS" if v["passed"] else "FAIL"
        print(f"{flag} {key:30} cases {v['cases']:3} routing {v['routing']:.0%} "
              f"adversarial {v['adversarial']} code checks {v['code_checks']}")
        for m in v["misses"]:
            print("   miss", json.dumps(m, ensure_ascii=False))
    for arm, v in rep["order_lines"].items():
        print(f"order lines, {arm}: {v['right']}/{v['lines']} right, clarification rate when "
              f"one sku fits {v['clarification_rate_when_one_sku']:.0%}, wrongly not carried "
              f"{v['said_not_carried_wrongly']}")
    print(f"skills eval: {rep['skills']} skills, {rep['cases']} cases, live cost "
          f"N{rep['live_kobo'] / 100:,.2f}; "
          f"{'PASSED' if rep['passed'] else 'FAILED'}")
    if not a.no_write:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if rep["passed"] else 1


__all__ = ["MIN_CASES", "cases_path", "load_cases", "main", "score"]
