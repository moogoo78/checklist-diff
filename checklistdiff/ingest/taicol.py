"""Read a TaiCOL name export.

TaiCOL (Taiwan Catalogue of Life) publishes two exports. The *taxon* export is
one row per accepted taxon with synonyms packed into a comma-joined column and
no authorship on them; the *name* export used here is one row per name, carrying
`usage_status` and the `taxon_id` it is placed under. The name export is the
richer source, but three of its conventions defeat a declarative field map:

1. A name placed in more than one taxon carries *parallel comma-joined lists* in
   `usage_status` and `taxon_id` — `"accepted,misapplied"` against
   `"t0052515,t0086584"` means one accepted usage plus one misapplied usage. Such
   a row has to become several `SourceRow`s, which a field map cannot express.

2. Rows are keyed by `name_id`, but a synonym points at its accepted name only
   indirectly, through the shared `taxon_id`. Resolving that means a first pass
   over the file to learn which `name_id` is accepted for each `taxon_id`.

3. `usage_status` is blank for names that exist nomenclaturally but are not
   placed in any taxon (~19% of rows). They are loaded with status `unknown`,
   which is what `normalize_status` returns for an empty value — they are real
   content of the release, and dropping them would make the next diff report
   them as removed.

The export has no parent pointer, only the denormalised `kingdom`…`genus`
columns, so a parent is derived from the deepest of those columns lying strictly
above the row's own rank. The derivation is approximate for ranks with no column
of their own (a Tribe gets its Family), but it is applied identically to every
release, so a diff still only reports placements that really moved.

That parent is emitted as a `name_id`, never as a name string. The loader
resolves an ID with a dict lookup but resolves a name by calling the parser, and
`NameParser.parse` spawns one gnparser process per name — so handing it ~96k
parent names would turn a minutes-long ingest into an hours-long one.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, TextIO

from checklistdiff.ingest.rows import SourceRow
from checklistdiff.models.enums import TaxonomicStatus

log = logging.getLogger(__name__)

# TaiCOL's own status vocabulary. "not-accepted" is how it spells synonymy;
# left unmapped it would normalise to UNKNOWN and every synonym in the database
# would lose its status.
STATUS_VALUES = {
    "accepted": TaxonomicStatus.ACCEPTED.value,
    "not-accepted": TaxonomicStatus.SYNONYM.value,
    "misapplied": TaxonomicStatus.MISAPPLIED.value,
}

# The denormalised classification columns, broad to narrow.
CLASS_COLS = ("kingdom", "phylum", "class", "order", "family", "genus")

# Index into CLASS_COLS of the deepest column lying strictly above each rank.
# Ranks absent here (Species and everything below it) fall back to genus.
_PARENT_COL: dict[str, int] = {
    "superkingdom": -1, "realm": -1, "kingdom": -1,
    "subkingdom": 0, "infrakingdom": 0, "superphylum": 0, "phylum": 0,
    "subphylum": 1, "infraphylum": 1, "superclass": 1, "megaclass": 1, "class": 1,
    "subclass": 2, "infraclass": 2, "superorder": 2, "order": 2,
    "suborder": 3, "infraorder": 3, "superfamily": 3, "epifamily": 3, "family": 3,
    "subfamily": 4, "tribe": 4, "subtribe": 4, "genus": 4,
    "subgenus": 5, "section": 5, "subsection": 5, "series": 5,
}

# Columns worth keeping in `usage.source_data`. The export has 50 columns and
# most are already modelled elsewhere; storing all of them would add a redundant
# JSON blob to every one of a quarter-million usages.
_KEEP_RAW = (
    "name_id", "taxon_id", "usage_status", "nomenclature_name", "namecode",
    "original_name_id", "protologue", "is_hybrid", "is_in_taiwan", "is_endemic",
    "alien_type", "cites", "iucn", "redlist", "protected", "sensitive",
)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _split(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


@contextmanager
def _open_csv(path: Path) -> Iterator[TextIO]:
    """Open the export whether it arrives as a bare .csv or a zipped one.

    The zip holds a plain CSV, not a Darwin Core Archive — there is no meta.xml,
    so the DwC-A reader cannot be used on it.
    """
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not members:
                raise ValueError(f"{path}: no .csv member in archive")
            with zf.open(members[0]) as raw:
                yield io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
    else:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            yield fh


def _index(
    path: Path,
) -> tuple[dict[str, str], dict[tuple[str, str, str], str], dict[str, str]]:
    """First pass: build the lookups that let every link be made by ID.

    Returns `(accepted_by_taxon, parent_by_rank, primary_taxon)`:

    * `taxon_id -> name_id of the accepted name`, because the file links a
      synonym to its accepted name only through a shared taxon. Resolving by
      name string instead would silently pick the wrong target whenever the same
      canonical name is accepted in one taxon and a synonym in another.

    * `(rank, name, kingdom) -> name_id`, so a parent drawn from the
      denormalised classification columns can be given as an ID.

    * `name_id -> taxon_id of its accepted usage`, which decides *which* of a
      name's usages gets the bare name_id as its anchor. Picking that by
      position instead would tie the anchor to the order of the comma-joined
      lists, and TaiCOL does not keep that order stable: four names across the
      2024 and 2026 releases move their accepted usage from second to first,
      which would silently show up in the diff as a removal plus an addition.

    Both matter for speed as much as correctness: the loader resolves a link by
    ID with a dict lookup, but resolves one given as a *name* by calling the
    parser, and `NameParser.parse` spawns one gnparser process per name. Handing
    it ~96k parent names to parse individually takes hours.
    """
    accepted: dict[str, str] = {}
    primary: dict[str, str] = {}
    has_accepted: set[str] = set()
    candidates: list[tuple[tuple[str, str, str], str]] = []

    with _open_csv(path) as fh:
        for row in csv.DictReader(fh):
            name_id = _clean(row.get("name_id"))
            simple_name = _clean(row.get("simple_name"))
            if not name_id or not simple_name:
                continue

            statuses = _split(row.get("usage_status"))
            taxa = _split(row.get("taxon_id"))
            for status, taxon_id in zip(statuses, taxa):
                if status == "accepted":
                    accepted.setdefault(taxon_id, name_id)

            if statuses and len(statuses) == len(taxa):
                own = next(
                    (t for st, t in zip(statuses, taxa) if st == "accepted"), None
                )
                if own is not None:
                    has_accepted.add(name_id)
                primary.setdefault(name_id, own or taxa[0])

            rank = (_clean(row.get("rank")) or "").lower()
            if rank in CLASS_COLS:
                kingdom = _clean(row.get("kingdom")) or ""
                candidates.append(((rank, simple_name, kingdom), name_id))

    # A parent is addressed by the name's *bare* name_id, and `_anchor` gives
    # that to the accepted usage — so only names that have one can be pointed
    # at this way.
    parents: dict[tuple[str, str, str], str] = {}
    for key, name_id in candidates:
        if name_id in has_accepted:
            parents.setdefault(key, name_id)

    return accepted, parents, primary


def _anchor(
    name_id: str, taxon_id: str | None, primary: dict[str, str], seen: set[str]
) -> str:
    """Return a key for this usage, unique within the release.

    `usage.source_taxon_id` is unique per (release, id), but a TaiCOL name_id is
    not unique per usage. A name placed in several taxa is usually one row with
    parallel comma-joined lists, but the export also just repeats the row
    sometimes — `Homalium fagifolium` appears twice in the 2025 release under one
    name_id *and* one taxon_id, once `not-accepted` and once `misapplied`.

    The name's *accepted* usage keeps the bare name_id — chosen by content, not
    by position, so a release that reorders the lists does not move the anchor.
    Other usages are qualified by taxon, and only if that still repeats by a
    counter.
    """
    qualified = f"{name_id}#{taxon_id or 'x'}"
    candidate = name_id if primary.get(name_id, taxon_id) == taxon_id else qualified
    if candidate in seen:
        candidate = qualified
    if candidate in seen:
        suffix = 2
        while f"{candidate}#{suffix}" in seen:
            suffix += 1
        candidate = f"{candidate}#{suffix}"
    seen.add(candidate)
    return candidate


def _parent_id(
    row: dict[str, Any],
    rank: str | None,
    own_name: str | None,
    parents: dict[tuple[str, str, str], str],
) -> str | None:
    """name_id of the nearest classification entry strictly above `rank`."""
    start = _PARENT_COL.get((rank or "").strip().lower(), len(CLASS_COLS) - 1)
    kingdom = _clean(row.get("kingdom")) or ""
    for index in range(start, -1, -1):
        column = CLASS_COLS[index]
        value = _clean(row.get(column))
        # A Genus row carries its own name in the `genus` column; that is the
        # row itself, not its parent.
        if not value or value == own_name:
            continue
        # Keyed on kingdom too: the same genus-rank name is used in more than
        # one kingdom, and the ranks alone would not tell them apart.
        if hit := parents.get((column, value, kingdom)):
            return hit
    return None


def read_rows(path: Path) -> Iterator[SourceRow]:
    """Yield `SourceRow`s from a TaiCOL name export (.csv or zipped .csv)."""
    accepted_by_taxon, parents, primary = _index(path)
    log.info(
        "taicol: %d taxa with an accepted name, %d addressable parents",
        len(accepted_by_taxon),
        len(parents),
    )

    seen: set[str] = set()

    with _open_csv(path) as fh:
        for row in csv.DictReader(fh):
            name_id = _clean(row.get("name_id"))
            simple_name = _clean(row.get("simple_name"))
            if not simple_name:
                continue

            rank = _clean(row.get("rank"))
            classification = {
                col: value
                for col in CLASS_COLS
                if (value := _clean(row.get(col)))
            }

            vernacular = [
                {"name": name, "language": "zh-TW"}
                for column in ("common_name_c", "alternative_name_c")
                for name in _split(row.get(column))
            ]

            raw = {k: v for k in _KEEP_RAW if (v := _clean(row.get(k)))}

            statuses = _split(row.get("usage_status"))
            taxa = _split(row.get("taxon_id"))

            # An unplaced name: no taxon, no status. Emitted as a single usage
            # so the release stays a faithful copy of the file.
            if not statuses:
                yield SourceRow(
                    source_taxon_id=(
                        _anchor(name_id, None, primary, seen) if name_id else None
                    ),
                    scientific_name=simple_name,
                    authorship=_clean(row.get("name_author")),
                    rank=rank,
                    status_raw=None,
                    parent_taxon_id=_parent_id(row, rank, simple_name, parents),
                    classification=classification,
                    vernacular=vernacular,
                    raw=raw,
                )
                continue

            if len(statuses) != len(taxa):
                # Parallel lists that do not line up cannot be split safely;
                # surfacing the row beats guessing which taxon each status meant.
                yield SourceRow(
                    source_taxon_id=name_id,
                    scientific_name=simple_name,
                    error=(
                        f"name_id {name_id}: usage_status has {len(statuses)} "
                        f"entries but taxon_id has {len(taxa)}"
                    ),
                )
                continue

            for status, taxon_id in zip(statuses, taxa):
                accepted_id = accepted_by_taxon.get(taxon_id)
                is_accepted = status == "accepted"

                yield SourceRow(
                    source_taxon_id=(
                        _anchor(name_id, taxon_id, primary, seen)
                        if name_id
                        else None
                    ),
                    scientific_name=simple_name,
                    authorship=_clean(row.get("name_author")),
                    rank=rank,
                    status_raw=STATUS_VALUES.get(status, status),
                    # Pointing an accepted usage at itself is meaningless; the
                    # loader would drop it anyway.
                    accepted_taxon_id=None if is_accepted else accepted_id,
                    parent_taxon_id=(
                        _parent_id(row, rank, simple_name, parents)
                        if is_accepted
                        else None
                    ),
                    classification=classification,
                    vernacular=vernacular,
                    raw=raw,
                )
