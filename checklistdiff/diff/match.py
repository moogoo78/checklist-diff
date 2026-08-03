"""Pass 1 — decide which usage in release N+1 *is* a given usage from release N.

Everything downstream depends on this answer, and it is the one genuinely
uncertain step in the pipeline. Two anchors are available:

`source_id`  the publisher's own taxonID. Correct when the publisher maintains
             stable identifiers, and worthless when they do not.
`name`       the normalised canonical name. Always available, but blind to
             renames by construction — the name *is* the anchor.

The choice is per-checklist (`checklist.id_stability`) and should come from
`ckdiff checklist check-ids`, not from optimism. Anchoring on IDs that get
renumbered between exports manufactures a diff consisting entirely of spurious
additions and removals, which is worse than useless: it buries the real changes.

Note that `canonical_key` is the name anchor, not `norm_key` — so a corrected
author string keeps the taxon's thread intact and is reported as
`author_changed` rather than shredding the history into a delete and an add.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from checklistdiff.models import (
    AnchorKind,
    Checklist,
    IdStability,
    Name,
    Release,
    Track,
    Usage,
    UsageTrack,
)

log = logging.getLogger(__name__)


@dataclass
class UsageInfo:
    """A usage plus the name facts the diff needs, fetched in one query."""

    usage_id: int
    source_taxon_id: str | None
    name_id: int
    canonical_key: str
    norm_key: str
    canonical_name: str
    authorship: str | None
    status: str
    rank: str | None
    accepted_usage_id: int | None
    parent_usage_id: int | None

    @property
    def anchor_by_name(self) -> str:
        return self.canonical_key


@dataclass
class MatchResult:
    anchor_kind: AnchorKind
    # anchor key -> usage, for each side
    before: dict[str, UsageInfo] = field(default_factory=dict)
    after: dict[str, UsageInfo] = field(default_factory=dict)
    matched: list[tuple[UsageInfo, UsageInfo]] = field(default_factory=list)
    added: list[UsageInfo] = field(default_factory=list)
    removed: list[UsageInfo] = field(default_factory=list)
    # Anchors that appeared more than once in one release; see `_index`.
    ambiguous: list[str] = field(default_factory=list)

    def by_id_before(self) -> dict[int, UsageInfo]:
        return {u.usage_id: u for u in self.before.values()}

    def by_id_after(self) -> dict[int, UsageInfo]:
        return {u.usage_id: u for u in self.after.values()}


def load_usages(session: Session, release_id: int) -> list[UsageInfo]:
    rows = session.execute(
        select(
            Usage.id,
            Usage.source_taxon_id,
            Usage.name_id,
            Name.canonical_key,
            Name.norm_key,
            Name.canonical_name,
            Name.authorship,
            Usage.status,
            Usage.rank,
            Usage.accepted_usage_id,
            Usage.parent_usage_id,
        )
        .join(Name, Name.id == Usage.name_id)
        .where(Usage.release_id == release_id)
    ).all()
    return [UsageInfo(*row) for row in rows]


def choose_anchor(checklist: Checklist, usages: list[UsageInfo]) -> AnchorKind:
    """Pick the anchor, honouring the checklist setting but refusing the absurd."""
    if checklist.id_stability == IdStability.PERSISTENT.value:
        # A checklist marked persistent whose IDs are actually absent would
        # otherwise match nothing at all.
        if any(u.source_taxon_id for u in usages):
            return AnchorKind.SOURCE_ID
        log.warning(
            "checklist %s is marked id_stability=persistent but has no taxon IDs; "
            "falling back to name anchoring",
            checklist.code,
        )
    return AnchorKind.NAME


def _index(
    usages: list[UsageInfo], anchor: AnchorKind, ambiguous: list[str]
) -> dict[str, UsageInfo]:
    """Build anchor -> usage, recording anchors that collide.

    A collision means the anchor does not uniquely identify a taxon in that
    release — two rows with the same canonical name, say, one accepted and one a
    synonym. Silently keeping either one would make the diff depend on file
    order, so an accepted usage is preferred deterministically and the collision
    is reported.
    """
    buckets: dict[str, list[UsageInfo]] = defaultdict(list)
    for usage in usages:
        key = usage.source_taxon_id if anchor is AnchorKind.SOURCE_ID else usage.anchor_by_name
        if key:
            buckets[key].append(usage)

    index: dict[str, UsageInfo] = {}
    for key, members in buckets.items():
        if len(members) == 1:
            index[key] = members[0]
            continue
        ambiguous.append(key)
        accepted = [m for m in members if m.status == "accepted"]
        pool = accepted or members
        # Lowest usage id: stable across runs regardless of read order.
        index[key] = min(pool, key=lambda m: m.usage_id)
    return index


def match_releases(
    session: Session,
    checklist: Checklist,
    before: Release,
    after: Release,
) -> MatchResult:
    """Pair up the usages of two releases."""
    before_usages = load_usages(session, before.id)
    after_usages = load_usages(session, after.id)

    anchor = choose_anchor(checklist, before_usages + after_usages)
    result = MatchResult(anchor_kind=anchor)

    result.before = _index(before_usages, anchor, result.ambiguous)
    result.after = _index(after_usages, anchor, result.ambiguous)

    for key, old in result.before.items():
        new = result.after.get(key)
        if new is None:
            result.removed.append(old)
        else:
            result.matched.append((old, new))

    for key, new in result.after.items():
        if key not in result.before:
            result.added.append(new)

    if result.ambiguous:
        log.warning(
            "%d ambiguous anchor(s) under %s anchoring, e.g. %s",
            len(result.ambiguous),
            anchor.value,
            result.ambiguous[:3],
        )

    log.info(
        "matched %s %s->%s on %s: %d pairs, %d added, %d removed",
        checklist.code,
        before.version,
        after.version,
        anchor.value,
        len(result.matched),
        len(result.added),
        len(result.removed),
    )
    return result


def persist_tracks(
    session: Session,
    checklist: Checklist,
    result: MatchResult,
    before: Release,
    after: Release,
) -> dict[int, Track]:
    """Materialise tracks and their memberships. Returns {usage_id -> Track}.

    Tracks are keyed by (checklist, anchor_kind, anchor_key) and reused across
    diff runs, so re-running a diff does not duplicate them.
    """
    anchor = result.anchor_kind
    keys = set(result.before) | set(result.after)

    existing = {
        t.anchor_key: t
        for t in session.scalars(
            select(Track).where(
                Track.checklist_id == checklist.id,
                Track.anchor_kind == anchor.value,
                Track.anchor_key.in_(list(keys)),
            )
        ).all()
    }

    for key in keys:
        if key not in existing:
            track = Track(
                checklist_id=checklist.id, anchor_kind=anchor.value, anchor_key=key
            )
            session.add(track)
            existing[key] = track
    session.flush()

    # Refresh memberships for these two releases only, so a re-run is idempotent
    # without disturbing other releases' threading.
    release_ids = [before.id, after.id]
    session.query(UsageTrack).filter(UsageTrack.release_id.in_(release_ids)).delete(
        synchronize_session=False
    )

    by_usage: dict[int, Track] = {}
    for side, release in ((result.before, before), (result.after, after)):
        for key, usage in side.items():
            track = existing[key]
            session.add(
                UsageTrack(
                    usage_id=usage.usage_id, track_id=track.id, release_id=release.id
                )
            )
            by_usage[usage.usage_id] = track
    session.flush()
    return by_usage
