"""Controlled vocabularies.

Stored as plain TEXT with a CHECK constraint rather than SQLAlchemy Enum: the
values stay readable in a raw `sqlite3` session, and adding a member is a CHECK
change rather than a type migration.
"""

from __future__ import annotations

from enum import StrEnum


class IdStability(StrEnum):
    """How much the checklist's own taxon IDs can be trusted across releases.

    Drives which anchoring strategy the diff engine uses. Verify with
    `ckdiff checklist check-ids` rather than assuming.
    """

    PERSISTENT = "persistent"  # IDs are stable identifiers; anchor on them
    UNSTABLE = "unstable"  # IDs exist but get renumbered; anchor on names
    NONE = "none"  # no IDs at all; anchor on names


class SourceFormat(StrEnum):
    DWCA = "dwca"
    CSV = "csv"
    XLSX = "xlsx"


class ReleaseStatus(StrEnum):
    IMPORTING = "importing"
    READY = "ready"
    FAILED = "failed"


class TaxonomicStatus(StrEnum):
    """Normalised from the source's own vocabulary during ingest."""

    ACCEPTED = "accepted"
    SYNONYM = "synonym"
    AMBIGUOUS_SYNONYM = "ambiguous_synonym"
    MISAPPLIED = "misapplied"
    PROVISIONAL = "provisional"
    BARE_NAME = "bare_name"
    UNKNOWN = "unknown"

    @property
    def is_synonymous(self) -> bool:
        """True when the usage should point at an accepted usage."""
        return self in {
            TaxonomicStatus.SYNONYM,
            TaxonomicStatus.AMBIGUOUS_SYNONYM,
            TaxonomicStatus.MISAPPLIED,
        }


class NomenclaturalCode(StrEnum):
    BOTANICAL = "botanical"
    ZOOLOGICAL = "zoological"
    BACTERIAL = "bacterial"
    VIRAL = "viral"
    UNKNOWN = "unknown"


class AnchorKind(StrEnum):
    SOURCE_ID = "source_id"
    NAME = "name"


class ChangeType(StrEnum):
    """The typed vocabulary of what can happen to a taxon between releases."""

    # Pass 1 — presence
    ADDED = "added"
    REMOVED = "removed"

    # Pass 2 — attribute changes on a matched pair
    RENAMED = "renamed"
    AUTHOR_CHANGED = "author_changed"
    STATUS_CHANGED = "status_changed"
    ACCEPTED_CHANGED = "accepted_changed"
    RECLASSIFIED = "reclassified"
    RANK_CHANGED = "rank_changed"

    # Pass 3 — set-level inference
    LUMPED = "lumped"
    SPLIT = "split"
    ID_REPLACED = "id_replaced"
    PROBABLE_RENAME = "probable_rename"

    @property
    def is_advisory(self) -> bool:
        """Inferred rather than asserted — needs human review before trusting."""
        return self is ChangeType.PROBABLE_RENAME


class LinkRelation(StrEnum):
    """Cross-checklist relations that shared name identity cannot express."""

    FUZZY_MATCH = "fuzzy_match"  # machine-suggested orthographic variant
    CONGRUENT = "congruent"  # curated: same concept
    INCLUDES = "includes"
    INCLUDED_IN = "included_in"
    OVERLAPS = "overlaps"
    DISJOINT = "disjoint"


def check_in(column: str, enum_cls: type[StrEnum]) -> str:
    """Render a CHECK expression restricting `column` to the enum's values."""
    values = ", ".join(f"'{m.value}'" for m in enum_cls)
    return f"{column} IN ({values})"
