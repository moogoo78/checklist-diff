"""Pass 2 — what changed about a taxon that exists in both releases.

Each matched pair is compared attribute by attribute. A pair can emit several
changes at once: sinking a species into synonymy is both a `status_changed` and
an `accepted_changed`, and reporting only one of them would understate what
happened.

Comparisons that dereference a pointer (`accepted_usage_id`, `parent_usage_id`)
resolve to the *name* on the other end, never the raw usage id. Usage ids are
per-release surrogates, so comparing them directly would flag every synonym in
the checklist as repointed on every release.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from checklistdiff.diff.match import MatchResult, UsageInfo
from checklistdiff.models import ChangeType


@dataclass(slots=True)
class PendingChange:
    """A change decided by a pass, before it becomes a database row."""

    type: ChangeType
    from_usage_id: int | None = None
    to_usage_id: int | None = None
    anchor_key: str | None = None
    detail: dict[str, Any] | None = None
    confidence: float | None = None


def _resolved_name(
    usage_id: int | None, by_id: dict[int, UsageInfo]
) -> tuple[str | None, str | None]:
    """Return (canonical_key, canonical_name) of a pointed-at usage."""
    if usage_id is None:
        return None, None
    target = by_id.get(usage_id)
    if target is None:
        return None, None
    return target.canonical_key, target.canonical_name


def compare_pair(
    old: UsageInfo,
    new: UsageInfo,
    before_by_id: dict[int, UsageInfo],
    after_by_id: dict[int, UsageInfo],
    anchor_key: str,
) -> Iterator[PendingChange]:
    """Yield every attribute-level change between one matched pair."""

    def change(kind: ChangeType, **detail: Any) -> PendingChange:
        return PendingChange(
            type=kind,
            from_usage_id=old.usage_id,
            to_usage_id=new.usage_id,
            anchor_key=anchor_key,
            detail=detail,
        )

    # --- the name itself ---------------------------------------------------
    # Under name anchoring these two are unreachable by construction: the
    # canonical name is the anchor, so a pair that matched must share it. Renames
    # surface instead as an added/removed pair, partly recovered by pass 3.
    if old.canonical_key != new.canonical_key:
        yield change(
            ChangeType.RENAMED,
            from_name=old.canonical_name,
            to_name=new.canonical_name,
        )
    elif old.norm_key != new.norm_key:
        # Same canonical name, different authorship. Worth its own type: it is
        # usually a correction rather than a taxonomic act, and users filtering
        # for "real" changes want to exclude it.
        yield change(
            ChangeType.AUTHOR_CHANGED,
            name=new.canonical_name,
            from_author=old.authorship,
            to_author=new.authorship,
        )

    # --- status ------------------------------------------------------------
    if old.status != new.status:
        yield change(
            ChangeType.STATUS_CHANGED,
            name=new.canonical_name,
            from_status=old.status,
            to_status=new.status,
        )

    # --- accepted-name pointer --------------------------------------------
    # The change downstream data owners care about most: a synonym quietly
    # repointed at a different accepted name silently re-identifies their records.
    old_acc_key, old_acc_name = _resolved_name(old.accepted_usage_id, before_by_id)
    new_acc_key, new_acc_name = _resolved_name(new.accepted_usage_id, after_by_id)
    if old_acc_key != new_acc_key:
        yield change(
            ChangeType.ACCEPTED_CHANGED,
            name=new.canonical_name,
            from_accepted=old_acc_name,
            to_accepted=new_acc_name,
        )

    # --- placement ---------------------------------------------------------
    old_par_key, old_par_name = _resolved_name(old.parent_usage_id, before_by_id)
    new_par_key, new_par_name = _resolved_name(new.parent_usage_id, after_by_id)
    if old_par_key != new_par_key:
        yield change(
            ChangeType.RECLASSIFIED,
            name=new.canonical_name,
            from_parent=old_par_name,
            to_parent=new_par_name,
        )

    # --- rank --------------------------------------------------------------
    if (old.rank or None) != (new.rank or None):
        yield change(
            ChangeType.RANK_CHANGED,
            name=new.canonical_name,
            from_rank=old.rank,
            to_rank=new.rank,
        )


def compare_all(result: MatchResult) -> list[PendingChange]:
    """Run pass 2 over every matched pair."""
    before_by_id = result.by_id_before()
    after_by_id = result.by_id_after()

    # Recover the anchor key for each pair so changes can be tied to a track.
    anchor_of = {
        usage.usage_id: key for key, usage in result.after.items()
    } | {usage.usage_id: key for key, usage in result.before.items()}

    changes: list[PendingChange] = []
    for old, new in result.matched:
        anchor = anchor_of.get(new.usage_id) or anchor_of.get(old.usage_id) or ""
        changes.extend(
            compare_pair(old, new, before_by_id, after_by_id, anchor)
        )
    return changes
