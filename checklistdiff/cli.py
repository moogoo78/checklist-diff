"""`ckdiff` command line entry point."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
import sys

import typer
from sqlalchemy import inspect, text

from checklistdiff import __version__
from checklistdiff.commands import checklist_cmd, diff_cmd, ingest_cmd, trace_cmd
from checklistdiff.config import get_settings
from checklistdiff.db import get_engine

app = typer.Typer(
    name="ckdiff",
    help="Track taxonomic checklists across releases and trace scientific names.",
    no_args_is_help=True,
)
app.add_typer(checklist_cmd.app, name="checklist")
app.add_typer(diff_cmd.app, name="diff")
app.command("ingest")(ingest_cmd.ingest)
app.command("trace")(trace_cmd.trace)


@app.callback()
def _main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


@app.command()
def version() -> None:
    """Print the ChecklistDiff version."""
    typer.echo(__version__)


@app.command()
def health() -> None:
    """Check that the database, its pragmas, and gnparser are all usable."""
    settings = get_settings()
    problems: list[str] = []

    typer.echo(f"checklistdiff {__version__}")
    typer.echo(f"python        {sys.version.split()[0]}")
    typer.echo(f"sqlite        {sqlite3.sqlite_version}")

    # FTS5 backs name lookup; a SQLite built without it fails much later and far
    # more confusingly, so check it up front.
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
        conn.close()
        typer.echo("fts5          available")
    except sqlite3.OperationalError as exc:
        problems.append(f"SQLite lacks FTS5 support: {exc}")
        typer.echo("fts5          MISSING")

    # gnparser
    binary = shutil.which(settings.gnparser_bin) or settings.gnparser_bin
    try:
        out = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        typer.echo(f"gnparser      {out.stdout.strip() or 'ok'}")
    except (OSError, subprocess.SubprocessError) as exc:
        problems.append(f"gnparser not runnable at {binary!r}: {exc}")
        typer.echo(f"gnparser      MISSING ({binary})")

    # Database
    typer.echo(f"database      {settings.db_path}")
    try:
        engine = get_engine()
        with engine.connect() as conn_sa:
            journal = conn_sa.execute(text("PRAGMA journal_mode")).scalar()
            fks = conn_sa.execute(text("PRAGMA foreign_keys")).scalar()
        tables = sorted(inspect(engine).get_table_names())
        typer.echo(f"  journal_mode  {journal}")
        typer.echo(f"  foreign_keys  {'ON' if fks else 'OFF'}")
        typer.echo(f"  tables        {len(tables)}")
        if not fks:
            problems.append("foreign_keys pragma did not stick")
        if not tables:
            typer.echo("  (no tables yet — run `alembic upgrade head`)")
    except Exception as exc:  # noqa: BLE001 - health check reports, never raises
        problems.append(f"database unusable: {exc}")

    if problems:
        typer.echo("")
        for p in problems:
            typer.secho(f"FAIL: {p}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.echo("")
    typer.secho("all checks passed", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
