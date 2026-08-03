from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy.orm import Session

from checklistdiff import config, db
from checklistdiff.models import Base, Checklist, IdStability

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.sqlite"


@pytest.fixture
def engine(db_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A throwaway database with the full schema.

    Built with `create_all` plus the FTS5 DDL rather than by running Alembic:
    tests should fail when a *model* is wrong, not when a migration is slow.
    `test_migrations.py` covers the migration path itself.
    """
    monkeypatch.setenv("CKDIFF_DB_PATH", str(db_path))
    config.reset_settings()
    db.reset_engine()

    eng = db.get_engine()
    Base.metadata.create_all(eng)
    _create_fts(eng)

    yield eng

    db.reset_engine()
    config.reset_settings()


def _create_fts(eng) -> None:
    """Mirror of the FTS5 DDL in the initial migration."""
    from sqlalchemy import text

    stmts = [
        """CREATE VIRTUAL TABLE name_fts USING fts5(
               canonical_name, content='name', content_rowid='id',
               tokenize='unicode61 remove_diacritics 2')""",
        """CREATE TRIGGER name_fts_ai AFTER INSERT ON name BEGIN
               INSERT INTO name_fts(rowid, canonical_name)
               VALUES (new.id, new.canonical_name); END""",
        """CREATE TRIGGER name_fts_ad AFTER DELETE ON name BEGIN
               INSERT INTO name_fts(name_fts, rowid, canonical_name)
               VALUES ('delete', old.id, old.canonical_name); END""",
        """CREATE TRIGGER name_fts_au AFTER UPDATE ON name BEGIN
               INSERT INTO name_fts(name_fts, rowid, canonical_name)
               VALUES ('delete', old.id, old.canonical_name);
               INSERT INTO name_fts(rowid, canonical_name)
               VALUES (new.id, new.canonical_name); END""",
    ]
    with eng.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))


@pytest.fixture
def session(engine) -> Iterator[Session]:
    with db.session_scope() as s:
        yield s


@pytest.fixture
def checklist(session: Session) -> Checklist:
    c = Checklist(
        code="synth",
        title="Synthetic Test Checklist",
        id_stability=IdStability.PERSISTENT.value,
    )
    session.add(c)
    session.flush()
    return c


@pytest.fixture(scope="session")
def gnparser_available() -> bool:
    import shutil
    import subprocess

    binary = os.environ.get("CKDIFF_GNPARSER_BIN", "gnparser")
    if not shutil.which(binary):
        return False
    try:
        subprocess.run([binary, "--version"], capture_output=True, timeout=10, check=True)
        return True
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _require_gnparser(request, gnparser_available: bool) -> None:
    """Skip parser-dependent tests when gnparser is missing rather than erroring."""
    if request.node.get_closest_marker("needs_gnparser") and not gnparser_available:
        pytest.skip("gnparser binary not available")
