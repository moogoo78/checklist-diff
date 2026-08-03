"""The migrations must produce exactly the schema the models describe.

This exists because they silently diverged once: a column was added to a model
and to the initial migration, but a database built from the *old* migration kept
working under `create_all`-based tests and only failed at runtime. Comparing the
two schemas directly closes that gap.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from checklistdiff.models import Base

REPO = Path(__file__).resolve().parent.parent


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["alembic", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={
            "PATH": "/venv/bin:/usr/local/bin:/usr/bin:/bin",
            "CKDIFF_DB_PATH": str(db_path),
            "PYTHONPATH": str(REPO),
        },
    )


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    db = tmp_path / "migrated.sqlite"
    result = _alembic(db, "upgrade", "head")
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade failed:\n{result.stderr}")
    return db


def _schema(engine) -> dict[str, set[str]]:
    inspector = inspect(engine)
    return {
        table: {c["name"] for c in inspector.get_columns(table)}
        for table in inspector.get_table_names()
        # FTS5 shadow tables are managed by SQLite and have no model counterpart.
        if not table.startswith("name_fts") and table != "alembic_version"
    }


def test_migrations_match_the_models(migrated: Path, tmp_path: Path) -> None:
    from_migration = _schema(create_engine(f"sqlite:///{migrated}"))

    declared_path = tmp_path / "declared.sqlite"
    declared_engine = create_engine(f"sqlite:///{declared_path}")
    Base.metadata.create_all(declared_engine)
    from_models = _schema(declared_engine)

    assert from_migration.keys() == from_models.keys(), (
        "tables differ between migration and models; "
        "run `alembic revision --autogenerate`"
    )
    for table in sorted(from_models):
        assert from_migration[table] == from_models[table], (
            f"columns of {table!r} differ between migration and models"
        )


def test_migration_round_trips(tmp_path: Path) -> None:
    db = tmp_path / "roundtrip.sqlite"
    assert _alembic(db, "upgrade", "head").returncode == 0
    down = _alembic(db, "downgrade", "base")
    assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"
    assert _alembic(db, "upgrade", "head").returncode == 0

    tables = set(_schema(create_engine(f"sqlite:///{db}")))
    assert "name" in tables and "usage" in tables


def test_fts_table_and_triggers_exist_after_migration(migrated: Path) -> None:
    from sqlalchemy import text

    engine = create_engine(f"sqlite:///{migrated}")
    with engine.connect() as conn:
        objects = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")
            )
        }
    assert "name_fts" in objects
    # Without all three the external-content index drifts out of sync with `name`
    # silently, rather than erroring.
    assert {"name_fts_ai", "name_fts_ad", "name_fts_au"} <= objects
