"""Orchestrate the three diff passes and persist the result.

Change rows are derived data. A diff run for a release pair always deletes and
rebuilds that pair's rows, so improving the algorithm never requires re-ingesting
a source file — which is the entire reason `usage` is append-only.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from checklistdiff.diff import attrs, setops
from checklistdiff.diff.attrs import PendingChange
from checklistdiff.diff.match import match_releases, persist_tracks
from checklistdiff.models import (
    Change,
    ChangeType,
    Checklist,
    Release,
    Track,
)

log = logging.getLogger(__name__)


class DiffError(RuntimeError):
    pass


@dataclass
class DiffReport:
    checklist_code: str
    from_version: str
    to_version: str
    anchor_kind: str
    counts: Counter[str] = field(default_factory=Counter)
    ambiguous_anchors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def summary(self) -> str:
        if not self.counts:
            return "no changes"
        parts = [f"{count} {kind}" for kind, count in self.counts.most_common()]
        return ", ".join(parts)


def _resolve_release(session: Session, checklist: Checklist, version: str) -> Release:
    release = session.scalar(
        select(Release).where(
            Release.checklist_id == checklist.id, Release.version == version
        )
    )
    if release is None:
        available = [r.version for r in checklist.releases]
        raise DiffError(
            f"{checklist.code} has no release {version!r} (have: {available})"
        )
    return release


def _existing_change_count(session: Session, before: Release, after: Release) -> int:
    return len(
        session.scalars(
            select(Change.id).where(
                Change.from_release_id == before.id, Change.to_release_id == after.id
            )
        ).all()
    )


def run_diff(
    session: Session,
    checklist: Checklist,
    from_version: str,
    to_version: str,
    *,
    force: bool = False,
    fuzzy_threshold: float | None = None,
) -> DiffReport:
    before = _resolve_release(session, checklist, from_version)
    after = _resolve_release(session, checklist, to_version)

    if before.id == after.id:
        raise DiffError("cannot diff a release against itself")

    existing = _existing_change_count(session, before, after)
    if existing and not force:
        raise DiffError(
            f"{checklist.code} {from_version}->{to_version} already has {existing} "
            "change rows; re-run with --force to recompute"
        )
    if existing:
        session.execute(
            delete(Change).where(
                Change.from_release_id == before.id, Change.to_release_id == after.id
            )
        )
        session.flush()

    # Pass 1
    match = match_releases(session, checklist, before, after)
    tracks_by_usage = persist_tracks(session, checklist, match, before, after)

    pending: list[PendingChange] = []

    # Pass 2
    pending.extend(attrs.compare_all(match))

    # Pass 3 — before emitting bare added/removed, so their findings can suppress
    # the pairs they explain.
    set_level = setops.run(match, fuzzy_threshold)
    pending.extend(set_level)

    # `id_replaced` is a certain explanation of an add/remove pair, so those rows
    # are suppressed. `probable_rename` is a guess and suppresses nothing — the
    # add and remove stay, with the suggestion recorded alongside them.
    explained_from = {
        c.from_usage_id for c in set_level if c.type is ChangeType.ID_REPLACED
    }
    explained_to = {
        c.to_usage_id for c in set_level if c.type is ChangeType.ID_REPLACED
    }

    for usage in match.added:
        if usage.usage_id in explained_to:
            continue
        pending.append(
            PendingChange(
                type=ChangeType.ADDED,
                to_usage_id=usage.usage_id,
                anchor_key=_anchor_of(match, usage.usage_id),
                detail={"name": usage.canonical_name, "status": usage.status},
            )
        )

    for usage in match.removed:
        if usage.usage_id in explained_from:
            continue
        pending.append(
            PendingChange(
                type=ChangeType.REMOVED,
                from_usage_id=usage.usage_id,
                anchor_key=_anchor_of(match, usage.usage_id),
                detail={"name": usage.canonical_name, "status": usage.status},
            )
        )

    # Persist
    track_by_key = _tracks_by_key(session, checklist, match.anchor_kind.value)
    report = DiffReport(
        checklist_code=checklist.code,
        from_version=from_version,
        to_version=to_version,
        anchor_kind=match.anchor_kind.value,
        ambiguous_anchors=match.ambiguous,
    )

    for change in pending:
        track = _track_for(change, track_by_key, tracks_by_usage)
        session.add(
            Change(
                checklist_id=checklist.id,
                from_release_id=before.id,
                to_release_id=after.id,
                track_id=track.id if track else None,
                type=change.type.value,
                from_usage_id=change.from_usage_id,
                to_usage_id=change.to_usage_id,
                detail=change.detail,
                confidence=change.confidence,
            )
        )
        report.counts[change.type.value] += 1

    session.flush()
    log.info(
        "diff %s %s->%s (%s anchoring): %s",
        checklist.code,
        from_version,
        to_version,
        match.anchor_kind.value,
        report.summary(),
    )
    return report


def _anchor_of(match, usage_id: int) -> str | None:
    for side in (match.after, match.before):
        for key, usage in side.items():
            if usage.usage_id == usage_id:
                return key
    return None


def _tracks_by_key(
    session: Session, checklist: Checklist, anchor_kind: str
) -> dict[str, Track]:
    return {
        t.anchor_key: t
        for t in session.scalars(
            select(Track).where(
                Track.checklist_id == checklist.id, Track.anchor_kind == anchor_kind
            )
        ).all()
    }


def _track_for(
    change: PendingChange,
    by_key: dict[str, Track],
    by_usage: dict[int, Track],
) -> Track | None:
    """Attach a change to a track where one applies.

    Lump and split span several tracks, so they carry no single track_id — their
    participants live in `detail` instead.
    """
    if change.type in (ChangeType.LUMPED, ChangeType.SPLIT):
        return None
    if change.anchor_key and (track := by_key.get(change.anchor_key)):
        return track
    for usage_id in (change.to_usage_id, change.from_usage_id):
        if usage_id is not None and (track := by_usage.get(usage_id)):
            return track
    return None
