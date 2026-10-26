"""Module rules: the runtime sits at the bottom and never reaches up.

``mimi.runtime`` may import only the standard library, third-party packages
and ``mimi.types``. Nothing in ``mimi`` imports an HTTP server framework.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "mimi"


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


def test_runtime_imports_nothing_above_it() -> None:
    for path in (ROOT / "runtime").rglob("*.py"):
        for name in _imports(path):
            if name.startswith("mimi."):
                assert name.startswith(("mimi.runtime", "mimi.types")), (path, name)


def test_no_http_server_framework() -> None:
    banned = ("fastapi", "flask", "django", "starlette", "uvicorn")
    for path in ROOT.rglob("*.py"):
        for name in _imports(path):
            assert name.split(".")[0] not in banned, (path, name)


def test_world_and_models_do_not_import_the_flow() -> None:
    for sub in ("world", "models"):
        for path in (ROOT / sub).rglob("*.py"):
            for name in _imports(path):
                assert not name.startswith(("mimi.receipts", "mimi.sim")), (path, name)


def test_voice_does_not_import_the_flow() -> None:
    """The speech port sits under the receipts; only its eval drives them."""
    for path in (ROOT / "voice").rglob("*.py"):
        if path.name == "eval.py":
            continue
        for name in _imports(path):
            assert not name.startswith(("mimi.receipts", "mimi.sim")), (path, name)
