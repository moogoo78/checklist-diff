"""Runtime configuration, read from the environment.

Kept dependency-free on purpose: every setting has a working default, so a bare
`ckdiff health` works outside Docker without an .env file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_PREFIX = "CKDIFF_"


def _env(name: str, default: str) -> str:
    return os.environ.get(ENV_PREFIX + name, default)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    gnparser_bin: str
    log_level: str

    # How many rows to accumulate before a bulk INSERT flush during ingest.
    ingest_batch_size: int

    # How many names to hand gnparser in one subprocess invocation.
    parse_batch_size: int

    # rapidfuzz score (0-100) below which a fuzzy candidate is discarded.
    fuzzy_threshold: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=Path(_env("DB_PATH", "data/checklistdiff.sqlite")),
            gnparser_bin=_env("GNPARSER_BIN", "gnparser"),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            ingest_batch_size=int(_env("INGEST_BATCH_SIZE", "5000")),
            parse_batch_size=int(_env("PARSE_BATCH_SIZE", "10000")),
            fuzzy_threshold=float(_env("FUZZY_THRESHOLD", "88")),
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reset_settings() -> None:
    """Drop the cached Settings so tests can re-read a patched environment."""
    global _settings
    _settings = None
