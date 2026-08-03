"""`ckdiff trace` — show one name's whole recorded life."""

from __future__ import annotations

import json

import typer

from checklistdiff.db import session_scope
from checklistdiff.trace.resolve import MatchTier, resolve
from checklistdiff.trace.timeline import build_trace, trace_to_dict

STATUS_COLOUR = {
    "accepted": "green",
    "synonym": "yellow",
    "ambiguous_synonym": "yellow",
    "misapplied": "red",
}


def trace(
    name: str = typer.Argument(..., help="Scientific name to trace."),
    as_json: bool = typer.Option(False, "--json"),
    threshold: float | None = typer.Option(None, "--fuzzy-threshold"),
) -> None:
    """Trace a scientific name across every checklist and release."""
    with session_scope() as session:
        resolution = resolve(session, name, threshold=threshold)

        if not resolution.found:
            typer.secho(f"no match for {name!r}", fg="red", err=True)
            raise typer.Exit(code=1)

        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "query": resolution.query,
                        "match": resolution.tier.value,
                        "traces": [
                            trace_to_dict(build_trace(session, n))
                            for n in resolution.names
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return

        if resolution.tier is MatchTier.FUZZY:
            typer.secho(
                f"no exact match; showing {len(resolution.names)} fuzzy result(s)",
                fg="yellow",
            )
        elif resolution.ambiguous:
            typer.secho(
                f"{len(resolution.names)} names share this canonical name "
                "(homonyms or author variants)",
                fg="yellow",
            )

        for name_row in resolution.names:
            _render(session, name_row, resolution)


def _render(session, name_row, resolution) -> None:
    t = build_trace(session, name_row)

    header = t.name
    if score := (resolution.scores or {}).get(name_row.id):
        header += f"   [fuzzy {score}]"
    typer.echo("")
    typer.secho(header, bold=True)
    typer.echo(f"  rank: {t.rank or '—'}    in {t.checklist_count} checklist(s)")

    if not t.checklists:
        typer.echo("  (present as a name, but no release uses it)")
        return

    for c in t.checklists:
        typer.echo("")
        typer.secho(f"  {c.checklist_code}  ({c.checklist_title})", fg="cyan")
        typer.echo(f"    anchoring: {c.anchor_kind or '—'}")
        for a in c.appearances:
            on = a.released_on.isoformat() if a.released_on else "—"
            typer.echo(f"    {a.release_version:<10} {on:<12} ", nl=False)
            typer.secho(a.status, fg=STATUS_COLOUR.get(a.status, "white"), nl=False)
            if a.accepted_name:
                typer.echo(f" of {a.accepted_name}", nl=False)
            typer.echo("")
            for ch in a.changes:
                marker = "~" if ch["advisory"] else "•"
                score = f" [{ch['confidence']}]" if ch["confidence"] else ""
                typer.secho(
                    f"        {marker} {ch['type']}{score}", fg="magenta"
                )

    if t.related:
        typer.echo("")
        typer.secho("  also published as:", fg="yellow")
        for r in t.related:
            typer.echo(f"    {r['scientific_name']}  ({r['reason']})")
