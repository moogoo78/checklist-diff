"""Turn `SourceRow`s into `Name` and `Usage` rows.

Three phases, in this order for a reason:

1. Parse every distinct name once and upsert `Name` rows.
2. Insert `Usage` rows with `name_id` but with both self-FKs left NULL.
3. Resolve `accepted_usage_id` / `parent_usage_id` in a second pass.

Phase 3 is separate because a synonym can point at an accepted usage that
appears *later* in the file — resolving inline would need the row to exist before
it has been read. Deferring also means the self-FKs can be resolved by ID or by
name without changing the insert path.

Rows are held in memory. At the scale this project targets (thousands to low
hundreds of thousands of usages per release) that is a few hundred MB at worst
and buys a great deal of simplicity; a streaming two-pass loader would be needed
only for a global backbone, which is explicitly out of scope.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from checklistdiff.config import get_settings
from checklistdiff.db import chunked
from checklistdiff.ingest.rows import SourceRow
from checklistdiff.models import (
    Checklist,
    Name,
    Release,
    ReleaseStatus,
    TaxonomicStatus,
    Usage,
)
from checklistdiff.naming.parse import NameParser

log = logging.getLogger(__name__)


class IngestError(RuntimeError):
    pass


@dataclass
class IngestReport:
    release_id: int
    version: str
    rows_read: int = 0
    usages_written: int = 0
    names_created: int = 0
    names_reused: int = 0
    accepted_resolved: int = 0
    parent_resolved: int = 0
    skipped: list[str] = field(default_factory=list)
    unresolved_accepted: list[str] = field(default_factory=list)
    unresolved_parent: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{self.rows_read} rows read",
            f"{self.usages_written} usages",
            f"{self.names_created} new names ({self.names_reused} reused)",
        ]
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        if self.unresolved_accepted:
            parts.append(f"{len(self.unresolved_accepted)} unresolved accepted")
        if self.unresolved_parent:
            parts.append(f"{len(self.unresolved_parent)} unresolved parent")
        return ", ".join(parts)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _upsert_names(
    session: Session,
    rows: Sequence[SourceRow],
    report: IngestReport,
    parser: NameParser,
) -> dict[str, Name]:
    """Parse all distinct name strings and return {verbatim -> Name}."""
    verbatims = [r.full_name for r in rows if r.full_name]
    parsed = parser.parse_many(verbatims)

    # Several verbatim strings can normalise to the same key ("Merr." vs "Merr"),
    # so collapse by norm_key before touching the database.
    by_key = {p.norm_key: p for p in parsed.values() if p.parsed_ok}

    # Chunked: a release can hold more distinct names than SQLite allows bind
    # parameters in a single statement.
    existing = {
        n.norm_key: n
        for chunk in chunked(by_key)
        for n in session.scalars(
            select(Name).where(Name.norm_key.in_(chunk))
        ).all()
    }
    report.names_reused = len(existing)

    for key, p in by_key.items():
        if key in existing:
            continue
        name = Name(
            scientific_name=p.verbatim,
            canonical_name=p.canonical_name,
            authorship=p.authorship,
            rank=p.rank,
            rank_marker=p.rank_marker,
            genus=p.genus,
            specific_epithet=p.specific_epithet,
            infraspecific_epithet=p.infraspecific_epithet,
            nomenclatural_code=p.nomenclatural_code,
            norm_key=p.norm_key,
            canonical_key=p.canonical_key,
            parsed=p.raw,
            parse_quality=p.quality,
            parse_warnings="; ".join(p.warnings) or None,
        )
        session.add(name)
        existing[key] = name
        report.names_created += 1

    session.flush()

    return {
        verbatim: existing[p.norm_key]
        for verbatim, p in parsed.items()
        if p.parsed_ok and p.norm_key in existing
    }


def _resolve_links(
    session: Session,
    rows: Sequence[SourceRow],
    usages: list[Usage],
    names: dict[str, Name],
    report: IngestReport,
    parser: NameParser,
) -> None:
    """Second pass: fill in accepted_usage_id and parent_usage_id."""
    by_source_id: dict[str, Usage] = {}
    by_canonical: dict[str, Usage] = {}

    for row, usage in zip(rows, usages, strict=True):
        if row.source_taxon_id:
            by_source_id[row.source_taxon_id] = usage
        name = names.get(row.full_name)
        # Accepted usages win the canonical index: when a name appears as both an
        # accepted taxon and a synonym elsewhere in the file, a bare name
        # reference means the accepted one.
        if name and (
            name.canonical_key not in by_canonical
            or usage.status == TaxonomicStatus.ACCEPTED.value
        ):
            by_canonical[name.canonical_key] = usage

    # Links given as a name string have to be parsed before they can be matched,
    # and `NameParser.parse` is one gnparser process per call. Resolving them as
    # they come up costs a process per link, which is invisible on a small
    # fixture and dominates the whole ingest on a real checklist — a few hundred
    # thousand spawns. So collect them first and parse the distinct set in one
    # batch, exactly as `_upsert_names` already does for the names themselves.
    #
    # Only links that an ID cannot already resolve are collected: `find` tries
    # `by_source_id` first, and a source that supplies IDs never needs a parse.
    pending: set[str] = set()
    for row in rows:
        for taxon_id, name_str in (
            (row.accepted_taxon_id, row.accepted_scientific_name),
            (row.parent_taxon_id, row.parent_scientific_name),
        ):
            if name_str and not (taxon_id and taxon_id in by_source_id):
                pending.add(name_str.strip())

    # The parser is shared with `_upsert_names`, so a link naming a taxon that
    # is also a row in this file — the overwhelmingly common case — is already
    # cached and costs no subprocess at all.
    parsed_links = parser.parse_many(sorted(pending)) if pending else {}

    def find(taxon_id: str | None, name_str: str | None) -> Usage | None:
        if taxon_id and (hit := by_source_id.get(taxon_id)):
            return hit
        if name_str and (parsed := parsed_links.get(name_str.strip())):
            return by_canonical.get(parsed.canonical_key)
        return None

    for row, usage in zip(rows, usages, strict=True):
        if row.accepted_taxon_id or row.accepted_scientific_name:
            target = find(row.accepted_taxon_id, row.accepted_scientific_name)
            if target is None:
                report.unresolved_accepted.append(
                    f"{row.source_taxon_id or row.scientific_name} -> "
                    f"{row.accepted_taxon_id or row.accepted_scientific_name}"
                )
            elif target is not usage:
                # A record that points at itself as its own accepted name is a
                # common way of spelling "accepted"; leave the FK NULL there.
                usage.accepted_usage_id = target.id
                report.accepted_resolved += 1

        if row.parent_taxon_id or row.parent_scientific_name:
            target = find(row.parent_taxon_id, row.parent_scientific_name)
            if target is None:
                report.unresolved_parent.append(
                    f"{row.source_taxon_id or row.scientific_name} -> "
                    f"{row.parent_taxon_id or row.parent_scientific_name}"
                )
            elif target is not usage:
                usage.parent_usage_id = target.id
                report.parent_resolved += 1

    session.flush()


def ingest_release(
    session: Session,
    checklist: Checklist,
    version: str,
    rows: Iterable[SourceRow],
    *,
    source_format: str,
    source_uri: str | None = None,
    source_sha256: str | None = None,
    released_on: date | None = None,
    replace: bool = False,
) -> IngestReport:
    """Load one release. Idempotent per (checklist, version)."""
    settings = get_settings()

    prior = session.scalar(
        select(Release).where(
            Release.checklist_id == checklist.id, Release.version == version
        )
    )
    if prior is not None:
        if not replace:
            same = source_sha256 and prior.source_sha256 == source_sha256
            raise IngestError(
                f"release {checklist.code}/{version} already ingested"
                + (
                    " with identical content; nothing to do"
                    if same
                    else " from different content. Re-run with --replace to overwrite"
                )
            )
        log.warning("replacing existing release %s/%s", checklist.code, version)
        session.delete(prior)
        session.flush()

    release = Release(
        checklist_id=checklist.id,
        version=version,
        released_on=released_on,
        source_uri=source_uri,
        source_sha256=source_sha256,
        format=source_format,
        status=ReleaseStatus.IMPORTING.value,
    )
    session.add(release)
    session.flush()

    report = IngestReport(release_id=release.id, version=version)

    usable: list[SourceRow] = []
    for row in rows:
        report.rows_read += 1
        if row.error:
            report.skipped.append(f"row {report.rows_read}: {row.error}")
        elif not row.full_name:
            report.skipped.append(f"row {report.rows_read}: no scientific name")
        else:
            usable.append(row)

    if not usable:
        release.status = ReleaseStatus.FAILED.value
        session.flush()
        raise IngestError(f"no usable rows in source ({report.rows_read} read)")

    parser = NameParser()
    names = _upsert_names(session, usable, report, parser)

    usages: list[Usage] = []
    batch = settings.ingest_batch_size
    for index, row in enumerate(usable):
        name = names.get(row.full_name)
        if name is None:
            report.skipped.append(f"unparseable name: {row.full_name!r}")
            continue
        usage = Usage(
            release_id=release.id,
            source_taxon_id=row.source_taxon_id,
            name_id=name.id,
            status=row.status.value,
            rank=row.rank or name.rank,
            classification=row.classification or None,
            vernacular=row.vernacular or None,
            source_data=row.raw or None,
        )
        session.add(usage)
        usages.append(usage)
        if index % batch == batch - 1:
            session.flush()

    session.flush()

    # `usable` and `usages` must stay index-aligned for the link pass; drop any
    # rows whose name failed to parse.
    aligned = [r for r in usable if r.full_name in names]
    _resolve_links(session, aligned, usages, names, report, parser)

    report.usages_written = len(usages)
    release.usage_count = len(usages)
    release.status = ReleaseStatus.READY.value
    session.flush()

    log.info("ingested %s/%s: %s", checklist.code, version, report.summary())
    return report
