"""Module rules: the layering is enforced here, not by review.

The package map and the reasoning are in ``docs/ARCHITECTURE.md``. This file
turns the layering into rules the build checks. A violation fails CI with a
message that names the rule, the offending file and the fix.

Layers, from the bottom up:

* foundation leaves (``types``, ``tz``, ``clock``, ``owed``, ``flags``,
  ``provenance``, ``evalmarks``) import nothing else in ``mimi``.
* the runtime sits above the leaves only.
* the store, world and models are the durable/data layer.
* the harness, connectors and skills are the agent spine.
* the flows (receipts, proof, voice, stock, credit, orders, returns, checkin,
  brand, computer, sim) sit above the spine.
* the API is the only edge, and the only place an HTTP server framework may be
  imported.

Two rules record a violation that already exists rather than restructure
another area (see ``KNOWN_LAYER_VIOLATIONS`` and ``docs/ARCHITECTURE.md``).
They stay listed so a *new* crossing fails while the team decides the fix.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "mimi"

# --- the packages in ``mimi/`` and the root modules -----------------------------

FLOWS = frozenset({
    "receipts", "proof", "voice", "stock", "credit", "orders", "returns",
    "checkin", "brand", "computer", "sim",
})
SPINE = frozenset({"harness", "connectors", "skills"})
LEAVES = frozenset({
    "types", "tz", "clock", "owed", "flags", "provenance", "evalmarks",
})
DATA = frozenset({"store", "world", "models"})
PACKAGES = frozenset({
    "api", "brand", "checkin", "computer", "connectors", "credit", "harness",
    "identity", "models", "observe", "orders", "proof", "receipts", "returns", "runtime",
    "sim", "skills", "stock", "store", "voice", "world",
})
HTTP_FRAMEWORKS = frozenset({"fastapi", "flask", "django", "starlette", "uvicorn"})


@dataclass(frozen=True)
class Rule:
    """One import rule: a file in ``sources`` must not import any package in
    ``forbidden``. ``hint`` is printed when it breaks."""

    name: str
    sources: frozenset[str]
    forbidden: frozenset[str]
    hint: str


# Packages the harness integrates on purpose (brand tools, computer tasks) are
# not in its forbidden set; the flows it must never reach for are.
RULES: tuple[Rule, ...] = (
    Rule(
        name="identity-is-a-foundation-package",
        sources=frozenset({"identity"}),
        forbidden=(PACKAGES - {"identity", "types", "tz", "clock", "store"}) | {"<mimi>"},
        hint="identity answers who a caller is and what they may do: it reads the "
             "store and the clock, and never the API, the harness or a flow",
    ),
    Rule(
        name="runtime-imports-nothing-above-it",
        sources=frozenset({"runtime"}),
        forbidden=(PACKAGES - {"runtime", "types", "tz"}) | {"<mimi>"},
        hint="the runtime is the durable job engine at the bottom: import only "
             "mimi.runtime, mimi.types and mimi.tz",
    ),
    Rule(
        name="leaves-import-nothing-in-mimi",
        sources=LEAVES,
        forbidden=PACKAGES | {"<mimi>"},
        hint="a foundation leaf has no mimi dependencies; move the shared code "
             "down into the leaf or keep it where it belongs",
    ),
    Rule(
        name="only-the-api-imports-the-api",
        sources=PACKAGES - {"api"},
        forbidden=frozenset({"api"}),
        hint="nothing but mimi.api may import mimi.api (so the agent stays "
             "framework-free); depend on the interface instead, or move the "
             "code out of mimi.api",
    ),
    Rule(
        name="harness-does-not-import-the-flows",
        sources=frozenset({"harness"}),
        forbidden=frozenset({
            "receipts", "proof", "voice", "stock", "sim", "credit", "orders",
            "returns", "checkin",
        }),
        hint="the harness is the loop every helper runs on and sits below the "
             "flows; the flow calls into the harness, never the reverse",
    ),
    Rule(
        name="connectors-sit-below-the-harness-and-the-flows",
        sources=frozenset({"connectors"}),
        forbidden=frozenset({"harness", "api"}) | FLOWS | frozenset({"skills"}),
        hint="a connector is a leaf transport (mimi.connectors may read "
             "mimi.store, mimi.world and mimi.tz); it must not reach into the "
             "harness or a flow",
    ),
    Rule(
        name="store-does-not-import-the-api-or-the-flows",
        sources=frozenset({"store"}),
        forbidden=frozenset({"api"}) | FLOWS,
        hint="the store is the system of record and sits under everything; it "
             "may read the harness and the world, never a flow or the API",
    ),
    Rule(
        name="flows-do-not-import-the-api",
        sources=FLOWS,
        forbidden=frozenset({"api"}),
        hint="a flow is served by the API, never the other way; move the shared "
             "code below the API",
    ),
    Rule(
        name="world-and-models-do-not-import-the-flows",
        sources=frozenset({"world", "models"}),
        forbidden=FLOWS,
        hint="the world and the model port are the deterministic data layer; a "
             "flow depends on them, not the reverse",
    ),
    Rule(
        name="observe-sits-beside-the-runtime",
        sources=frozenset({"observe"}),
        forbidden=PACKAGES - {"observe", "runtime", "models", "types"},
        hint="tracing may read the runtime and the model port, never the flows",
    ),
    Rule(
        name="voice-does-not-import-the-receipt-flow",
        sources=frozenset({"voice"}),
        forbidden=frozenset({"receipts", "sim"}),
        hint="the speech port sits under the receipts; only its eval drives them",
    ),
    Rule(
        name="proof-does-not-import-the-sim",
        sources=frozenset({"proof"}),
        forbidden=frozenset({"sim"}),
        hint="Mimi reads screenshots; only the eval fixtures draw them",
    ),
    Rule(
        name="stock-does-not-import-the-sim-or-the-other-flows",
        sources=frozenset({"stock"}),
        forbidden=frozenset({"sim", "receipts", "proof", "voice"}),
        hint="the stock helper reads the world and the runtime and imports no "
             "other flow",
    ),
)

# Violations that exist today. They are listed (not fixed) so a new crossing
# fails while the owning area decides the fix. Each is also in
# docs/ARCHITECTURE.md, "Where reality breaks the layering".
#   ("world", "stock"): mimi/world/expiry.py uses the stock estimator to build
#       the slow-stock fixture. The world is data; the estimator is a flow.
#   ("store", "credit"): mimi/store/book.py imports mimi.credit.looks for two
#       pure name helpers. The store should not depend on a flow.
KNOWN_LAYER_VIOLATIONS: frozenset[tuple[str, str]] = frozenset({
    ("world", "stock"),
    ("store", "credit"),
})

# Eval drivers (``*/eval.py``, ``*_eval.py``, ``experiments.py``,
# ``review_pocs.py``) wire a flow into the models, the world and another flow on
# purpose, so the layering rules do not apply to them. Everything else is
# production code.
EVAL_DRIVER = re.compile(r"(^eval\.py$|_eval\.py$|^experiments\.py$|^review_pocs\.py$)")


def _pkg_of(path: Path) -> str:
    rel = path.relative_to(ROOT)
    if rel.parent == Path("."):
        return rel.stem
    return rel.parts[0]


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


def edges(root: Path = ROOT) -> Iterator[tuple[str, str, Path]]:
    """Every cross-package import: (source package, target package, file)."""
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        src = _pkg_of(path)
        for name in _imports(path):
            if name == "mimi" or name.startswith("mimi."):
                parts = name.split(".")
                dst = parts[1] if len(parts) > 1 else "<mimi>"
                if dst != src:
                    yield src, dst, path


def rule_violations(
    found: Iterable[tuple[str, str, Path]],
) -> list[tuple[Rule, str, str, Path]]:
    """Apply every rule to the given edges. Returns (rule, src, dst, file)."""
    out: list[tuple[Rule, str, str, Path]] = []
    for src, dst, path in found:
        if not path.is_absolute():
            path = ROOT / path
        if EVAL_DRIVER.search(path.name):
            continue
        for rule in RULES:
            if src in rule.sources and dst in rule.forbidden:
                out.append((rule, src, dst, path))
    return out


def _message(rule: Rule, src: str, dst: str, path: Path) -> str:
    rel = path.relative_to(ROOT.parent) if path.is_absolute() else path
    return (
        f"\n[layer-rules] rule '{rule.name}' broke\n"
        f"  {rel} (package '{src}') imports mimi.{dst}\n"
        f"  fix: {rule.hint}\n"
    )


# --- the rules the build runs ---------------------------------------------------


def test_the_layering_holds() -> None:
    """Every cross-package import obeys every rule, apart from the two
    violations already recorded in KNOWN_LAYER_VIOLATIONS."""
    breaks = [
        (rule, src, dst, path)
        for rule, src, dst, path in rule_violations(edges())
        if (src, dst) not in KNOWN_LAYER_VIOLATIONS
    ]
    assert not breaks, "".join(_message(*b) for b in breaks)


def test_the_checker_catches_a_bad_import() -> None:
    """A deliberately bad import trips the rule and names the fix, so the
    guardrail is real and not a test that passes on anything."""
    bad = [("runtime", "harness", Path("mimi/runtime/bad_example.py"))]
    breaks = rule_violations(bad)
    assert breaks, "a runtime -> harness import must be a violation"
    rule, src, dst, path = breaks[0]
    assert rule.name == "runtime-imports-nothing-above-it"
    assert (src, dst) == ("runtime", "harness")
    assert rule.name in _message(rule, src, dst, path)
    assert "fix:" in _message(rule, src, dst, path)


def test_only_the_api_imports_an_http_server_framework() -> None:
    for path in ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if (ROOT / "api") in path.parents:
            continue
        for name in _imports(path):
            assert name.split(".")[0] not in HTTP_FRAMEWORKS, (path, name)


def test_every_package_is_covered_by_a_rule() -> None:
    """A new package cannot be added without deciding its layer: this test
    fails until the package appears in PACKAGES and a rule."""
    present = {
        p.name for p in ROOT.iterdir()
        if p.is_dir() and p.name != "__pycache__"
        and next(p.glob("*.py"), None) is not None
    }
    missing = present - PACKAGES
    assert not missing, f"new mimi package(s) not in PACKAGES/rules: {sorted(missing)}"
