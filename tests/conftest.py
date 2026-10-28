"""Shared fixtures.

``store_url``: a fresh database for the store tests. A SQLite file per test
by default; when ``MIMI_TEST_DATABASE_URL`` is set (the Postgres job in CI),
that database, emptied first. Tests that use it run serially.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa


@pytest.fixture()
def store_url(tmp_path: Path) -> str:
    pg = os.environ.get("MIMI_TEST_DATABASE_URL", "").strip()
    if not pg:
        return f"sqlite:///{tmp_path / 'store.db'}"
    from mimi.store.db import normalize_url
    from mimi.store.schema import metadata

    engine = sa.create_engine(normalize_url(pg))
    metadata.drop_all(engine)
    engine.dispose()
    return pg
