"""The contract between readers and the loader.

Every reader (csvmap, dwca, …) turns its source into `SourceRow` objects. The
loader knows only this shape, so adding a new input format never touches the
loading, name-resolution, or diff code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from checklistdiff.models.enums import TaxonomicStatus

# Darwin Core's taxonomicStatus vocabulary is not actually controlled in
# practice: publishers write "accepted", "valid", "Accepted name", "synonym ",
# "homotypic synonym", "=", and so on. Everything is lowercased and stripped of
# punctuation before lookup here.
_STATUS_MAP: dict[str, TaxonomicStatus] = {
    "accepted": TaxonomicStatus.ACCEPTED,
    "acceptedname": TaxonomicStatus.ACCEPTED,
    "valid": TaxonomicStatus.ACCEPTED,
    "validname": TaxonomicStatus.ACCEPTED,
    "correct": TaxonomicStatus.ACCEPTED,
    "current": TaxonomicStatus.ACCEPTED,
    "synonym": TaxonomicStatus.SYNONYM,
    "homotypicsynonym": TaxonomicStatus.SYNONYM,
    "heterotypicsynonym": TaxonomicStatus.SYNONYM,
    "objectivesynonym": TaxonomicStatus.SYNONYM,
    "subjectivesynonym": TaxonomicStatus.SYNONYM,
    "juniorsynonym": TaxonomicStatus.SYNONYM,
    "invalid": TaxonomicStatus.SYNONYM,
    "ambiguoussynonym": TaxonomicStatus.AMBIGUOUS_SYNONYM,
    "misapplied": TaxonomicStatus.MISAPPLIED,
    "misappliedname": TaxonomicStatus.MISAPPLIED,
    "provisional": TaxonomicStatus.PROVISIONAL,
    "provisionallyaccepted": TaxonomicStatus.PROVISIONAL,
    "barename": TaxonomicStatus.BARE_NAME,
    "nomennudum": TaxonomicStatus.BARE_NAME,
}

_PUNCT = re.compile(r"[^a-z]")

# Ranks in broad-to-narrow order; used to build the denormalised classification.
CLASSIFICATION_RANKS = (
    "kingdom",
    "phylum",
    "class",
    "order",
    "family",
    "genus",
    "subgenus",
)


def normalize_status(raw: str | None) -> TaxonomicStatus:
    """Map a source's status string onto the controlled vocabulary.

    Unrecognised values become UNKNOWN rather than being guessed at — a wrong
    status silently changes what the diff engine reports about a taxon.
    """
    if not raw or not raw.strip():
        return TaxonomicStatus.UNKNOWN
    key = _PUNCT.sub("", raw.strip().lower())
    return _STATUS_MAP.get(key, TaxonomicStatus.UNKNOWN)


@dataclass(slots=True)
class SourceRow:
    """One taxon record as read from a source file, before any resolution."""

    # The publisher's own identifier. None when the source has no ID column;
    # the loader falls back to name-based linking in that case.
    source_taxon_id: str | None

    # Verbatim name string. If the source keeps authorship in its own column,
    # the reader appends it here as well as setting `authorship`, since gnparser
    # works on the whole string.
    scientific_name: str
    authorship: str | None = None
    rank: str | None = None

    status_raw: str | None = None

    # Links, expressed either by ID or by name. Readers set whichever the source
    # provides; the loader resolves ID first and falls back to name.
    accepted_taxon_id: str | None = None
    accepted_scientific_name: str | None = None
    parent_taxon_id: str | None = None
    parent_scientific_name: str | None = None

    classification: dict[str, str] = field(default_factory=dict)
    vernacular: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    # Populated by the reader when a row cannot be used, so the loader can report
    # it rather than failing the whole ingest.
    error: str | None = None

    @property
    def status(self) -> TaxonomicStatus:
        return normalize_status(self.status_raw)

    @property
    def full_name(self) -> str:
        """Name string to hand gnparser: verbatim name plus author if separate."""
        name = (self.scientific_name or "").strip()
        author = (self.authorship or "").strip()
        if author and author.lower() not in name.lower():
            return f"{name} {author}".strip()
        return name
