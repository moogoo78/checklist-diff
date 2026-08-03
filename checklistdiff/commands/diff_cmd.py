"""`ckdiff diff` — compute and inspect changes between two releases."""

from __future__ import annotations

import json

import typer
from sqlalchemy import select

from checklistdiff.db import session_scope
from checklistdiff.diff.engine import DiffError, run_diff
from checklistdiff.models import Change, ChangeType, Checklist, Release

app = typer.Typer(help="Compute and inspect release-to-release changes.", no_args_is_help=True)

# Grouped so the report reads as taxonomy first, bookkeeping last, rather than
# in whatever order the passes happened to emit.
DISPLAY_ORDER = [
    ChangeType.LUMPED,
    ChangeType.SPLIT,
    ChangeType.ACCEPTED_CHANGED,
    ChangeType.STATUS_CHANGED,
    ChangeType.RECLASSIFIED,
    ChangeType.RANK_CHANGED,
    ChangeType.RENAMED,
    ChangeType.AUTHOR_CHANGED,
    ChangeType.ADDED,
    ChangeType.REMOVED,
    ChangeType.ID_REPLACED,
    ChangeType.PROBABLE_RENAME,
]


def _checklist(session, code: str) -> Checklist:
    checklist = session.scalar(select(Checklist).where(Checklist.code == code))
    if checklist is None:
        raise typer.BadParameter(f"no checklist with code {code!r}")
    return checklist


@app.command("run")
def run(
    checklist_code: str = typer.Option(..., "--checklist"),
    from_version: str = typer.Option(..., "--from"),
    to_version: str = typer.Option(..., "--to"),
    force: bool = typer.Option(False, "--force", help="Recompute if already diffed."),
    threshold: float | None = typer.Option(
        None, "--fuzzy-threshold", help="Score 0-100 for probable_rename suggestions."
    ),
) -> None:
    """Diff two releases of a checklist."""
    with session_scope() as session:
        checklist = _checklist(session, checklist_code)
        try:
            report = run_diff(
                session,
                checklist,
                from_version,
                to_version,
                force=force,
                fuzzy_threshold=threshold,
            )
        except DiffError as exc:
            typer.secho(str(exc), fg="red", err=True)
            raise typer.Exit(code=1) from exc

        typer.secho(
            f"{checklist_code} {from_version} -> {to_version} "
            f"({report.anchor_kind} anchoring)",
            bold=True,
        )
        if not report.total:
            typer.echo("  no changes")
            return

        for kind in DISPLAY_ORDER:
            if count := report.counts.get(kind.value):
                marker = "  ~" if kind.is_advisory else "   "
                typer.echo(f"{marker} {count:>6}  {kind.value}")
        typer.echo(f"\n   {report.total:>6}  total")

        if any(k.is_advisory for k in DISPLAY_ORDER if report.counts.get(k.value)):
            typer.secho(
                "\n  ~ advisory: inferred, not asserted. Review before trusting.",
                fg="yellow",
            )
        if report.ambiguous_anchors:
            typer.secho(
                f"\n  {len(report.ambiguous_anchors)} ambiguous anchor(s): "
                f"{report.ambiguous_anchors[:5]}",
                fg="yellow",
            )


@app.command("show")
def show(
    checklist_code: str = typer.Option(..., "--checklist"),
    from_version: str = typer.Option(..., "--from"),
    to_version: str = typer.Option(..., "--to"),
    change_type: str | None = typer.Option(None, "--type", help="Filter by change type."),
    limit: int = typer.Option(50, "--limit"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List individual changes between two releases."""
    with session_scope() as session:
        checklist = _checklist(session, checklist_code)
        releases = {r.version: r for r in checklist.releases}
        for v in (from_version, to_version):
            if v not in releases:
                raise typer.BadParameter(f"{checklist_code} has no release {v!r}")

        stmt = select(Change).where(
            Change.from_release_id == releases[from_version].id,
            Change.to_release_id == releases[to_version].id,
        )
        if change_type:
            valid = {t.value for t in ChangeType}
            if change_type not in valid:
                raise typer.BadParameter(f"--type must be one of {sorted(valid)}")
            stmt = stmt.where(Change.type == change_type)

        changes = session.scalars(stmt.limit(limit)).all()

        if as_json:
            typer.echo(
                json.dumps(
                    [
                        {
                            "type": c.type,
                            "detail": c.detail,
                            "confidence": c.confidence,
                        }
                        for c in changes
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return

        if not changes:
            typer.echo("no changes match")
            return

        for c in changes:
            detail = c.detail or {}
            score = f"  [{c.confidence}]" if c.confidence is not None else ""
            typer.secho(f"{c.type}{score}", fg="cyan", nl=False)
            typer.echo("  " + _describe(c.type, detail))


def _describe(kind: str, d: dict) -> str:
    match kind:
        case "lumped":
            return f"{', '.join(d.get('sunk', []))} -> {d.get('into')}"
        case "split":
            return f"{d.get('from')} -> {', '.join(d.get('into', []))}"
        case "accepted_changed":
            return f"{d.get('name')}: {d.get('from_accepted')} -> {d.get('to_accepted')}"
        case "status_changed":
            return f"{d.get('name')}: {d.get('from_status')} -> {d.get('to_status')}"
        case "reclassified":
            return f"{d.get('name')}: parent {d.get('from_parent')} -> {d.get('to_parent')}"
        case "rank_changed":
            return f"{d.get('name')}: {d.get('from_rank')} -> {d.get('to_rank')}"
        case "renamed" | "probable_rename":
            return f"{d.get('from_name')} -> {d.get('to_name')}"
        case "author_changed":
            return f"{d.get('name')}: {d.get('from_author')} -> {d.get('to_author')}"
        case "id_replaced":
            return f"{d.get('name')}: {d.get('from_id')} -> {d.get('to_id')}"
        case _:
            return f"{d.get('name', '')} ({d.get('status', '')})".strip()
