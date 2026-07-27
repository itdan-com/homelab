"""Alembic environment — wired to Sentinel's app metadata.

Replaces the generated boilerplate with two Sentinel specifics:
  1. The DB URL comes from app.config (env var SENTINEL_DB), never
     from alembic.ini — one source of truth with the running app.
  2. target_metadata points at app.db.Base so `alembic revision
     --autogenerate` diffs real models (5.5.2 onward).

`render_as_batch=True`: SQLite can't ALTER most things in place;
batch mode makes Alembic rebuild-and-copy tables so future column
changes actually work. Set now so no one trips on it mid-phase.
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make `app` importable when alembic runs from the sentinel/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DB_URL  # noqa: E402
from app.db import Base  # noqa: E402
import app.models  # noqa: E402,F401  (ensure model classes register on Base)

config = context.config
config.set_main_option("sqlalchemy.url", DB_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
