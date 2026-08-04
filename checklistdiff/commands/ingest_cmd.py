"""`ckdiff ingest` — load one release of a checklist."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import typer
from sqlalchemy import select

from checklistdiff.db import analyze, bulk_load, session_scope
from checklistdiff.ingest import csvmap, dwca, taicol
from checklistdiff.ingest.loader import IngestError, file_sha256, ingest_release
from checklistdiff.models import Checklist, SourceFormat


def ingest(
    checklist_code: str = typer.Option(..., "--checklist", help="Checklist code."),
    version: str = typer.Option(..., "--version", help="Release version label."),
    file: Path = typer.Option(..., "--file", exists=True, readable=True),
    map_file: Path | None = typer.Option(
        None,
        "--map",
        exists=True,
        readable=True,
        help="YAML field map. Required for CSV/XLSX, ignored for a DwC-A.",
    ),
    released_on: str | None = typer.Option(
        None, "--released-on", help="Publication date, YYYY-MM-DD."
    ),
    replace: bool = typer.Option(
        False, "--replace", help="Overwrite this version if already ingested."
    ),
    reader: str = typer.Option(
        "auto",
        "--reader",
        help="auto | dwca | csv | taicol-name. 'auto' treats .zip as a DwC-A, "
        "anything else as CSV/XLSX.",
    ),
) -> None:
    """Ingest a release from a Darwin Core Archive, CSV, or spreadsheet."""
    readers = {"auto", "dwca", "csv", "taicol-name"}
    if reader not in readers:
        raise typer.BadParameter(f"--reader must be one of {sorted(readers)}")
    if reader == "auto":
        reader = "dwca" if file.suffix.lower() == ".zip" else "csv"

    if reader == "csv" and map_file is None:
        raise typer.BadParameter(
            "--map is required for CSV/XLSX input (a DwC-A carries its own meta.xml)"
        )

    published = None
    if released_on:
        try:
            published = date.fromisoformat(released_on)
        except ValueError as exc:
            raise typer.BadParameter(f"--released-on: {exc}") from exc

    digest = file_sha256(file)

    if reader == "dwca":
        rows = dwca.read_rows(file)
        source_format = SourceFormat.DWCA.value
    elif reader == "taicol-name":
        rows = taicol.read_rows(file)
        source_format = SourceFormat.CSV.value
    else:
        rows = csvmap.read_rows(file, map_file)  # type: ignore[arg-type]
        source_format = csvmap.detect_format(file)

    try:
        with bulk_load(), session_scope() as session:
            checklist = session.scalar(
                select(Checklist).where(Checklist.code == checklist_code)
            )
            if checklist is None:
                raise typer.BadParameter(
                    f"no checklist with code {checklist_code!r}; "
                    f"register it first with `ckdiff checklist add`"
                )

            report = ingest_release(
                session,
                checklist,
                version,
                rows,
                source_format=source_format,
                source_uri=str(file),
                source_sha256=digest,
                released_on=published,
                replace=replace,
            )
    except IngestError as exc:
        typer.secho(f"ingest failed: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc

    analyze()

    typer.secho(f"{checklist_code}/{version}: {report.summary()}", fg="green")

    # Surface data-quality problems rather than burying them — an unresolved
    # accepted-name pointer becomes an invisible gap in the next diff.
    for label, items in (
        ("skipped rows", report.skipped),
        ("unresolved accepted-name links", report.unresolved_accepted),
        ("unresolved parent links", report.unresolved_parent),
    ):
        if not items:
            continue
        typer.secho(f"\n{len(items)} {label}:", fg="yellow")
        for item in items[:10]:
            typer.echo(f"  {item}")
        if len(items) > 10:
            typer.echo(f"  … and {len(items) - 10} more")
