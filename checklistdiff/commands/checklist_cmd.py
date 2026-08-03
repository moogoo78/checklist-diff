"""`ckdiff checklist` — register checklists and inspect their ID stability."""

from __future__ import annotations

import typer
from sqlalchemy import select

from checklistdiff.db import session_scope
from checklistdiff.models import Checklist, IdStability, Release, Usage

app = typer.Typer(help="Register and inspect checklists.", no_args_is_help=True)


def _require(session, code: str) -> Checklist:
    checklist = session.scalar(select(Checklist).where(Checklist.code == code))
    if checklist is None:
        raise typer.BadParameter(f"no checklist with code {code!r}")
    return checklist


@app.command("add")
def add(
    code: str = typer.Option(..., "--code", help="Short unique key, e.g. 'taicol'."),
    title: str = typer.Option(..., "--title"),
    publisher: str | None = typer.Option(None, "--publisher"),
    homepage: str | None = typer.Option(None, "--homepage"),
    license_: str | None = typer.Option(None, "--license"),
    id_stability: str = typer.Option(
        IdStability.UNSTABLE.value,
        "--id-stability",
        help="persistent | unstable | none. Defaults to the pessimistic "
        "'unstable'; confirm with `check-ids` before changing it.",
    ),
) -> None:
    """Register a checklist."""
    if id_stability not in {s.value for s in IdStability}:
        raise typer.BadParameter(
            f"must be one of {[s.value for s in IdStability]}"
        )

    with session_scope() as session:
        if session.scalar(select(Checklist).where(Checklist.code == code)):
            raise typer.BadParameter(f"checklist {code!r} already exists")
        session.add(
            Checklist(
                code=code,
                title=title,
                publisher=publisher,
                homepage=homepage,
                license=license_,
                id_stability=id_stability,
            )
        )
    typer.secho(f"added checklist {code!r} (id_stability={id_stability})", fg="green")


@app.command("list")
def list_() -> None:
    """List registered checklists and their releases."""
    with session_scope() as session:
        checklists = session.scalars(select(Checklist).order_by(Checklist.code)).all()
        if not checklists:
            typer.echo("no checklists registered")
            return
        for c in checklists:
            typer.secho(f"{c.code}  {c.title}", bold=True)
            typer.echo(f"    id_stability: {c.id_stability}")
            if not c.releases:
                typer.echo("    (no releases)")
            for r in c.releases:
                on = r.released_on.isoformat() if r.released_on else "—"
                typer.echo(
                    f"    {r.version:<12} {on:<12} {r.usage_count:>7} usages  [{r.status}]"
                )


@app.command("set")
def set_(
    code: str = typer.Option(..., "--code"),
    id_stability: str = typer.Option(..., "--id-stability"),
) -> None:
    """Change a checklist's anchoring strategy."""
    if id_stability not in {s.value for s in IdStability}:
        raise typer.BadParameter(f"must be one of {[s.value for s in IdStability]}")
    with session_scope() as session:
        checklist = _require(session, code)
        old = checklist.id_stability
        checklist.id_stability = id_stability
    typer.secho(f"{code}: id_stability {old} -> {id_stability}", fg="green")


@app.command("check-ids")
def check_ids(
    code: str = typer.Option(..., "--code"),
    from_version: str = typer.Option(..., "--from"),
    to_version: str = typer.Option(..., "--to"),
) -> None:
    """Measure whether the publisher's taxon IDs survive between two releases.

    This is the evidence behind `id_stability`. Anchoring a diff on IDs that get
    renumbered produces thousands of spurious additions and removals, so the
    setting should be an observation, never an assumption.
    """
    with session_scope() as session:
        checklist = _require(session, code)
        releases = {r.version: r for r in checklist.releases}
        for v in (from_version, to_version):
            if v not in releases:
                raise typer.BadParameter(
                    f"{code} has no release {v!r} (have: {sorted(releases)})"
                )

        def ids(release: Release) -> set[str]:
            rows = session.scalars(
                select(Usage.source_taxon_id).where(
                    Usage.release_id == release.id,
                    Usage.source_taxon_id.is_not(None),
                )
            ).all()
            return set(rows)

        def keys(release: Release) -> set[str]:
            from checklistdiff.models import Name

            rows = session.execute(
                select(Name.canonical_key)
                .join(Usage, Usage.name_id == Name.id)
                .where(Usage.release_id == release.id)
            ).scalars()
            return set(rows)

        a, b = releases[from_version], releases[to_version]
        ids_a, ids_b = ids(a), ids(b)
        keys_a, keys_b = keys(a), keys(b)

        typer.secho(f"{code}: {from_version} -> {to_version}", bold=True)
        typer.echo(f"  usages          {a.usage_count} -> {b.usage_count}")

        if not ids_a or not ids_b:
            typer.echo("  taxon IDs       absent in at least one release")
            typer.secho(
                "  recommendation  --id-stability none  (nothing to anchor on)",
                fg="yellow",
            )
            return

        shared_ids = ids_a & ids_b
        shared_keys = keys_a & keys_b
        id_overlap = len(shared_ids) / min(len(ids_a), len(ids_b)) * 100
        name_overlap = (
            len(shared_keys) / min(len(keys_a), len(keys_b)) * 100 if keys_a and keys_b else 0
        )

        typer.echo(f"  IDs             {len(ids_a)} -> {len(ids_b)}")
        typer.echo(f"  ID overlap      {id_overlap:.1f}% ({len(shared_ids)} shared)")
        typer.echo(f"  name overlap    {name_overlap:.1f}% ({len(shared_keys)} shared)")

        # The verdict rests on the *gap*, not on absolute overlap. A checklist
        # can legitimately turn over half its taxa between releases; that is
        # taxonomic churn, and it lowers ID and name overlap equally. What
        # indicates renumbering is names surviving markedly better than IDs.
        gap = name_overlap - id_overlap
        if gap < 5:
            verdict, colour = IdStability.PERSISTENT.value, "green"
            note = (
                "IDs survive as well as names, so they carry real identity — "
                "and they detect renames, which name anchoring cannot"
            )
        elif gap < 20:
            verdict, colour = IdStability.UNSTABLE.value, "yellow"
            note = (
                f"names persist {gap:.1f} points better than IDs; some renumbering. "
                "Prefer name anchoring, but compare both before committing"
            )
        else:
            verdict, colour = IdStability.UNSTABLE.value, "red"
            note = (
                f"names persist {gap:.1f} points better than IDs; the IDs are being "
                "renumbered and anchoring on them would manufacture changes"
            )

        typer.echo("")
        typer.secho(f"  recommendation  --id-stability {verdict}", fg=colour)
        typer.echo(f"                  {note}")
        if verdict != checklist.id_stability:
            typer.echo(
                f"\n  currently set to {checklist.id_stability!r}; change with:\n"
                f"    ckdiff checklist set --code {code} --id-stability {verdict}"
            )
