"""Build the trace: one name's whole recorded life.

The output is organised checklist by checklist, and within each checklist
release by release, because that is how the question is actually asked — "what
did *this* checklist say about this name, over time, and did anyone else say
something different?"

Two things make this cheap. Usages point at a globally deduped `Name`, so
finding every checklist that mentions a name is one indexed join with no
matching step. And `track` is materialised, so a taxon's thread through releases
is a lookup rather than a recursive walk over change rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from checklistdiff.models import (
    Change,
    Checklist,
    Name,
    Release,
    Track,
    Usage,
    UsageTrack,
)


@dataclass
class Appearance:
    """What one release said about the name."""

    release_version: str
    released_on: date | None
    status: str
    rank: str | None
    scientific_name: str
    authorship: str | None
    accepted_name: str | None
    parent_name: str | None
    classification: dict[str, str] | None
    usage_id: int
    # Changes recorded on the way *into* this release.
    changes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ChecklistTrace:
    checklist_code: str
    checklist_title: str
    id_stability: str
    anchor_kind: str | None
    appearances: list[Appearance] = field(default_factory=list)

    @property
    def first_seen(self) -> str | None:
        return self.appearances[0].release_version if self.appearances else None

    @property
    def last_seen(self) -> str | None:
        return self.appearances[-1].release_version if self.appearances else None


@dataclass
class Trace:
    name: str
    canonical_name: str
    authorship: str | None
    rank: str | None
    name_id: int
    checklists: list[ChecklistTrace] = field(default_factory=list)
    # Other Name rows sharing this canonical name — homonyms, or the same name
    # under a different author string.
    related: list[dict[str, Any]] = field(default_factory=list)

    @property
    def checklist_count(self) -> int:
        return len(self.checklists)


def _display_name(session: Session, usage_id: int | None) -> str | None:
    if usage_id is None:
        return None
    row = session.execute(
        select(Name.scientific_name)
        .join(Usage, Usage.name_id == Name.id)
        .where(Usage.id == usage_id)
    ).first()
    return row[0] if row else None


def _changes_for(
    session: Session, usage_id: int, release_id: int
) -> list[dict[str, Any]]:
    """Changes landing on this usage as of its release."""
    rows = session.scalars(
        select(Change).where(
            Change.to_release_id == release_id, Change.to_usage_id == usage_id
        )
    ).all()
    return [
        {
            "type": c.type,
            "detail": c.detail,
            "confidence": c.confidence,
            "advisory": c.confidence is not None,
        }
        for c in rows
    ]


def build_trace(session: Session, name: Name) -> Trace:
    """Assemble the full trace for one `Name` row."""
    trace = Trace(
        name=name.scientific_name,
        canonical_name=name.canonical_name,
        authorship=name.authorship,
        rank=name.rank,
        name_id=name.id,
    )

    rows = session.execute(
        select(Usage, Release, Checklist)
        .join(Release, Release.id == Usage.release_id)
        .join(Checklist, Checklist.id == Release.checklist_id)
        .where(Usage.name_id == name.id)
        .order_by(Checklist.code, Release.released_on, Release.version)
    ).all()

    by_checklist: dict[str, ChecklistTrace] = {}
    for usage, release, checklist in rows:
        entry = by_checklist.get(checklist.code)
        if entry is None:
            anchor = session.scalar(
                select(Track.anchor_kind)
                .join(UsageTrack, UsageTrack.track_id == Track.id)
                .where(UsageTrack.usage_id == usage.id)
            )
            entry = ChecklistTrace(
                checklist_code=checklist.code,
                checklist_title=checklist.title,
                id_stability=checklist.id_stability,
                anchor_kind=anchor,
            )
            by_checklist[checklist.code] = entry

        entry.appearances.append(
            Appearance(
                release_version=release.version,
                released_on=release.released_on,
                status=usage.status,
                rank=usage.rank,
                scientific_name=name.scientific_name,
                authorship=name.authorship,
                accepted_name=_display_name(session, usage.accepted_usage_id),
                parent_name=_display_name(session, usage.parent_usage_id),
                classification=usage.classification,
                usage_id=usage.id,
                changes=_changes_for(session, usage.id, release.id),
            )
        )

    trace.checklists = list(by_checklist.values())

    # Same canonical name, different Name row: a homonym, or an author variant.
    # Surfacing these is the difference between "this name is not in TaiCOL" and
    # "TaiCOL has it under a different author".
    siblings = session.scalars(
        select(Name).where(
            Name.canonical_key == name.canonical_key, Name.id != name.id
        )
    ).all()
    trace.related = [
        {
            "name_id": s.id,
            "scientific_name": s.scientific_name,
            "authorship": s.authorship,
            "reason": "same canonical name, different authorship",
        }
        for s in siblings
    ]

    return trace


def trace_to_dict(trace: Trace) -> dict[str, Any]:
    return {
        "name": trace.name,
        "canonical_name": trace.canonical_name,
        "authorship": trace.authorship,
        "rank": trace.rank,
        "name_id": trace.name_id,
        "checklist_count": trace.checklist_count,
        "related": trace.related,
        "checklists": [
            {
                "code": c.checklist_code,
                "title": c.checklist_title,
                "id_stability": c.id_stability,
                "anchor_kind": c.anchor_kind,
                "first_seen": c.first_seen,
                "last_seen": c.last_seen,
                "appearances": [
                    {
                        "release": a.release_version,
                        "released_on": a.released_on.isoformat()
                        if a.released_on
                        else None,
                        "status": a.status,
                        "rank": a.rank,
                        "accepted_name": a.accepted_name,
                        "parent_name": a.parent_name,
                        "classification": a.classification,
                        "changes": a.changes,
                    }
                    for a in c.appearances
                ],
            }
            for c in trace.checklists
        ],
    }
