"""Alembic environment.

Two things here are load-bearing for SQLite:

- `render_as_batch=True`, because SQLite's ALTER TABLE cannot drop or alter a
  column; batch mode rewrites the table instead.
- the URL comes from CKDIFF_DB_PATH via our own settings, so migrations and the
  application can never disagree about which file they are pointed at.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from checklistdiff.config import get_settings
from checklistdiff.db import get_engine
from checklistdiff.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    settings = get_settings()
    context.configure(
        url=f"sqlite:///{settings.db_path}",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = get_engine()
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            # FTS5 shadow tables (name_fts_data, name_fts_idx, …) are managed by
            # SQLite, not by us — autogenerate must not try to drop them.
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


def _include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
    if type_ == "table" and (name.startswith("name_fts") or name == "sqlite_stat1"):
        return False
    return True


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
