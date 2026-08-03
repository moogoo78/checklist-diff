"""Engine construction and SQLite pragma management.

Everything funnels through `get_engine()` so the pragmas below are applied
uniformly — a connection that skipped `foreign_keys=ON` would silently accept
orphaned usages, which is exactly the corruption this schema is built to avoid.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from checklistdiff.config import get_settings

log = logging.getLogger(__name__)

# Applied to every new connection. journal_mode is persisted in the file itself,
# but setting it is idempotent and costs nothing.
RUNTIME_PRAGMAS = {
    "journal_mode": "WAL",
    "foreign_keys": "ON",
    "synchronous": "NORMAL",
    "busy_timeout": "10000",
    # Negative cache_size is KiB rather than pages; 64 MiB suits diff-time joins.
    "cache_size": "-64000",
    "temp_store": "MEMORY",
}

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _url_for(db_path: Path) -> str:
    if str(db_path) == ":memory:":
        return "sqlite+pysqlite:///:memory:"
    return f"sqlite+pysqlite:///{db_path}"


def _register_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # noqa: ANN001 - SQLAlchemy callback
        cursor = dbapi_conn.cursor()
        try:
            for pragma, value in RUNTIME_PRAGMAS.items():
                # WAL is meaningless for in-memory databases and SQLite ignores
                # the request, so there is no need to special-case it here.
                cursor.execute(f"PRAGMA {pragma}={value}")
        finally:
            cursor.close()


def get_engine(db_path: Path | None = None, *, echo: bool = False) -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is not None and db_path is None:
        return _engine

    settings = get_settings()
    path = db_path or settings.db_path
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        _url_for(path),
        echo=echo,
        future=True,
        # FastAPI serves requests from a thread pool; SQLite connections are
        # otherwise pinned to their creating thread.
        connect_args={"check_same_thread": False},
    )
    _register_pragmas(engine)

    if db_path is None:
        _engine = engine
    return engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session: commits on success, rolls back on exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def bulk_load(engine: Engine | None = None) -> Iterator[Engine]:
    """Relax durability pragmas for the duration of a bulk ingest.

    A crash inside this block can corrupt the database — that is the trade being
    made, and it is acceptable only because ingest is re-runnable from the source
    file. Never wrap interactive or partial writes in this.
    """
    engine = engine or get_engine()
    with engine.begin() as conn:
        conn.execute(text("PRAGMA synchronous=OFF"))
    try:
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA synchronous=NORMAL"))
            conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))


def analyze(engine: Engine | None = None) -> None:
    """Refresh planner statistics. Cheap, and worth running after every ingest."""
    engine = engine or get_engine()
    with engine.begin() as conn:
        conn.execute(text("ANALYZE"))


def reset_engine() -> None:
    """Dispose the cached engine and session factory (used by tests)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
