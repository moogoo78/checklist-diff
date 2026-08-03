"""Pass 3 — changes that are invisible when you look at one row at a time.

A row-by-row diff can tell you that *Testia delta* became a synonym and that
*Testia nu* became a synonym. It cannot tell you that they were **lumped** into
the same species — that fact lives in the relationship between rows, and it is
usually the change a taxonomist actually wants to read about.

Four inferences here:

`lumped`           several previously-accepted taxa now resolve to one accepted name
`split`            one previously-accepted taxon now resolves to several
`id_replaced`      an apparent delete+create that is really a renumbering
`probable_rename`  an apparent delete+create that is probably a spelling fix

The last two exist to suppress false `added`/`removed` pairs. They differ in
confidence and are typed differently for that reason: `id_replaced` is certain
(the name matched exactly), while `probable_rename` is a fuzzy guess that is
recorded as advisory and never silently applied.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from rapidfuzz import fuzz, process

from checklistdiff.config import get_settings
from checklistdiff.diff.attrs import PendingChange
from checklistdiff.diff.match import MatchResult, UsageInfo
from checklistdiff.models import AnchorKind, ChangeType, TaxonomicStatus

log = logging.getLogger(__name__)

ACCEPTED = TaxonomicStatus.ACCEPTED.value


def _effective_accepted(
    usage: UsageInfo, by_id: dict[int, UsageInfo]
) -> tuple[str, str] | None:
    """The (canonical_key, canonical_name) this usage ultimately resolves to.

    An accepted usage resolves to itself; a synonym resolves to its accepted
    usage. Returns None when a synonym's pointer is dangling, so callers can skip
    it rather than inventing a relationship.
    """
    if usage.status == ACCEPTED:
        return usage.canonical_key, usage.canonical_name
    if usage.accepted_usage_id is None:
        return None
    target = by_id.get(usage.accepted_usage_id)
    if target is None:
        return None
    return target.canonical_key, target.canonical_name


def detect_lumps(result: MatchResult) -> list[PendingChange]:
    """Several accepted taxa in N collapsing onto one accepted name in N+1."""
    before_by_id = result.by_id_before()
    after_by_id = result.by_id_after()

    # Group matched pairs by what the N+1 side resolves to, counting only taxa
    # that were accepted in N — a synonym moving between accepted names is an
    # `accepted_changed`, not a lump.
    groups: dict[str, list[tuple[UsageInfo, UsageInfo]]] = defaultdict(list)
    target_names: dict[str, str] = {}

    for old, new in result.matched:
        if old.status != ACCEPTED:
            continue
        resolved = _effective_accepted(new, after_by_id)
        if resolved is None:
            continue
        key, name = resolved
        groups[key].append((old, new))
        target_names[key] = name

    changes: list[PendingChange] = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        # All but the surviving taxon must actually have stopped being accepted;
        # otherwise this is not a lump, just several taxa that already shared an
        # accepted name.
        sunk = [(o, n) for o, n in members if n.status != ACCEPTED]
        if not sunk:
            continue
        changes.append(
            PendingChange(
                type=ChangeType.LUMPED,
                anchor_key=key,
                detail={
                    "into": target_names[key],
                    "sources": sorted(o.canonical_name for o, _ in members),
                    "sunk": sorted(o.canonical_name for o, _ in sunk),
                    "count": len(members),
                },
            )
        )
        _ = before_by_id  # symmetry with detect_splits; kept for clarity
    return changes


def detect_splits(result: MatchResult) -> list[PendingChange]:
    """One accepted taxon in N whose circumscription yields several in N+1.

    Detected by following the taxon *and its synonyms* forward: if a name that
    was a synonym of X in release N is accepted in its own right in N+1, X has
    been split.
    """
    before_by_id = result.by_id_before()
    after_by_id = result.by_id_after()

    # For each N-accepted usage, the set of N usages resolving to it (itself plus
    # its synonyms).
    circumscription: dict[int, list[UsageInfo]] = defaultdict(list)
    for usage in result.before.values():
        resolved_id = (
            usage.usage_id if usage.status == ACCEPTED else usage.accepted_usage_id
        )
        if resolved_id is not None and resolved_id in before_by_id:
            circumscription[resolved_id].append(usage)

    # Where did each N usage end up in N+1?
    forward: dict[int, UsageInfo] = {old.usage_id: new for old, new in result.matched}

    changes: list[PendingChange] = []
    for accepted_id, members in circumscription.items():
        source = before_by_id.get(accepted_id)
        if source is None or len(members) < 2:
            continue

        products: dict[str, str] = {}
        for member in members:
            new = forward.get(member.usage_id)
            if new is None:
                continue
            resolved = _effective_accepted(new, after_by_id)
            if resolved and new.status == ACCEPTED:
                products[resolved[0]] = resolved[1]

        if len(products) < 2:
            continue
        changes.append(
            PendingChange(
                type=ChangeType.SPLIT,
                anchor_key=source.canonical_key,
                detail={
                    "from": source.canonical_name,
                    "into": sorted(products.values()),
                    "count": len(products),
                },
            )
        )
    return changes


def detect_id_replacements(result: MatchResult) -> list[PendingChange]:
    """Suppress delete+create pairs that are really a renumbering.

    Only meaningful under source_id anchoring: the same name reappearing under a
    new taxonID is a bookkeeping change, not a taxonomic one, and reporting it as
    an addition plus a removal is actively misleading.
    """
    if result.anchor_kind is not AnchorKind.SOURCE_ID:
        return []

    removed_by_norm = {u.norm_key: u for u in result.removed}
    changes: list[PendingChange] = []
    consumed: set[int] = set()

    for new in result.added:
        old = removed_by_norm.get(new.norm_key)
        if old is None or old.usage_id in consumed:
            continue
        consumed.add(old.usage_id)
        changes.append(
            PendingChange(
                type=ChangeType.ID_REPLACED,
                from_usage_id=old.usage_id,
                to_usage_id=new.usage_id,
                anchor_key=new.source_taxon_id,
                detail={
                    "name": new.canonical_name,
                    "from_id": old.source_taxon_id,
                    "to_id": new.source_taxon_id,
                },
            )
        )
    return changes


def detect_probable_renames(
    result: MatchResult,
    threshold: float | None = None,
    explained: set[int] | None = None,
) -> list[PendingChange]:
    """Pair leftover additions and removals that look like spelling changes.

    This is the only place fuzzy matching touches the diff, and its output is
    explicitly advisory — recorded with a confidence score for review, never used
    to suppress the underlying `added`/`removed` rows. Asserting a rename on
    string similarity alone would fabricate taxonomic history.

    `explained` carries the usage ids that an earlier, *certain* detector has
    already accounted for (currently `id_replaced`). Without it a renumbered
    taxon would be reported twice — once correctly as a renumbering, and once as
    a 100%-confidence "rename" of a name to itself.
    """
    explained = explained or set()
    added = [u for u in result.added if u.usage_id not in explained]
    removed = [u for u in result.removed if u.usage_id not in explained]
    if not added or not removed:
        return []

    cutoff = threshold if threshold is not None else get_settings().fuzzy_threshold

    candidates = {u.canonical_key: u for u in removed}
    if not candidates:
        return []

    changes: list[PendingChange] = []
    consumed: set[str] = set()

    for new in added:
        pool = [
            k for k in candidates if k not in consumed and k != new.canonical_key
        ]
        if not pool:
            break
        hit = process.extractOne(
            new.canonical_key, pool, scorer=fuzz.ratio, score_cutoff=cutoff
        )
        if hit is None:
            continue
        key, score, _ = hit
        old = candidates[key]
        consumed.add(key)
        changes.append(
            PendingChange(
                type=ChangeType.PROBABLE_RENAME,
                from_usage_id=old.usage_id,
                to_usage_id=new.usage_id,
                anchor_key=new.canonical_key,
                detail={
                    "from_name": old.canonical_name,
                    "to_name": new.canonical_name,
                    "advisory": True,
                },
                confidence=round(float(score), 2),
            )
        )
    return changes


def run(result: MatchResult, threshold: float | None = None) -> list[PendingChange]:
    """Run pass 3. Order matters: certain explanations before fuzzy guesses."""
    changes: list[PendingChange] = []
    changes.extend(detect_lumps(result))
    changes.extend(detect_splits(result))

    replacements = detect_id_replacements(result)
    changes.extend(replacements)

    # Anything `id_replaced` accounted for is settled; the fuzzy matcher must not
    # offer a competing explanation for the same pair.
    explained = {c.from_usage_id for c in replacements if c.from_usage_id is not None}
    explained |= {c.to_usage_id for c in replacements if c.to_usage_id is not None}

    changes.extend(detect_probable_renames(result, threshold, explained))
    return changes
