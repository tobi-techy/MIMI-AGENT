"""Alembic environment for Mimi.

The migrations use ``op.create_table`` directly, so there is no ORM metadata
to compare against (``target_metadata = None``). The URL comes from the
``MIMI_DB_URL`` environment variable (any SQLAlchemy URL: SQLite for a local
try, Postgres with the ``postgres`` extra installed). It is never stored in
this repository.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

import sqlalchemy as sa

from alembic import context

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _url() -> str:
    url = os.environ.get("MIMI_DB_URL", "").strip()
    if not url:
        raise SystemExit(
            "Set MIMI_DB_URL to a SQLAlchemy URL, for example "
            "sqlite:////tmp/mimi.db"
        )
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = sa.create_engine(_url(), poolclass=sa.pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
