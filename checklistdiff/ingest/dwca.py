"""Read a Darwin Core Archive.

Driven by `meta.xml` rather than by fixed filenames or column positions, so any
conformant archive works without per-source configuration. `meta.xml` declares
the core file, its delimiters, and which DwC term each column index carries —
column *order* varies freely between publishers, so reading it is not optional.

Archives whose meta.xml is missing or unreadable fall back to a plain
`taxon.txt`/`Taxon.tsv` with a header row, since that covers most hand-rolled
exports.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree

from checklistdiff.ingest.rows import CLASSIFICATION_RANKS, SourceRow

log = logging.getLogger(__name__)

DWC_NS = "http://rs.tdwg.org/dwc/terms/"

# DwC term -> SourceRow attribute.
TERM_MAP = {
    "taxonID": "source_taxon_id",
    "scientificName": "scientific_name",
    "scientificNameAuthorship": "authorship",
    "taxonRank": "rank",
    "verbatimTaxonRank": "rank",
    "taxonomicStatus": "status_raw",
    "acceptedNameUsageID": "accepted_taxon_id",
    "acceptedNameUsage": "accepted_scientific_name",
    "parentNameUsageID": "parent_taxon_id",
    "parentNameUsage": "parent_scientific_name",
}

VERNACULAR_TERMS = ("vernacularName",)


class DwcaError(ValueError):
    pass


@dataclass
class CoreSpec:
    """The core file's layout, as declared in meta.xml."""

    location: str
    delimiter: str
    quote: str
    encoding: str
    skip_header: int
    # column index -> bare DwC term name
    columns: dict[int, str]
    id_index: int | None


def _term(uri: str) -> str:
    return uri.rsplit("/", 1)[-1] if uri else ""


def _parse_meta(xml_bytes: bytes) -> CoreSpec:
    root = ElementTree.fromstring(xml_bytes)
    ns = {"d": "http://rs.tdwg.org/dwc/text/"}

    core = root.find("d:core", ns)
    if core is None:
        # Some archives omit the namespace entirely.
        core = root.find("core")
    if core is None:
        raise DwcaError("meta.xml has no <core> element")

    files = core.find("d:files/d:location", ns)
    if files is None:
        files = core.find("files/location")
    if files is None or not files.text:
        raise DwcaError("meta.xml <core> declares no file location")

    columns: dict[int, str] = {}
    fields = core.findall("d:field", ns) or core.findall("field")
    for f in fields:
        idx = f.get("index")
        term = _term(f.get("term", ""))
        if idx is not None and term:
            columns[int(idx)] = term

    id_elem = core.find("d:id", ns)
    if id_elem is None:
        id_elem = core.find("id")
    id_index = int(id_elem.get("index")) if id_elem is not None and id_elem.get("index") else None

    delimiter = core.get("fieldsTerminatedBy", "\\t")
    quote = core.get("fieldsEnclosedBy", '"')
    return CoreSpec(
        location=files.text.strip(),
        delimiter=delimiter.replace("\\t", "\t").replace("\\n", "\n") or "\t",
        quote=quote,
        encoding=core.get("encoding", "UTF-8"),
        skip_header=int(core.get("ignoreHeaderLines", "0")),
        columns=columns,
        id_index=id_index,
    )


def _open_archive(path: Path) -> zipfile.ZipFile:
    if not zipfile.is_zipfile(path):
        raise DwcaError(f"{path} is not a zip archive")
    return zipfile.ZipFile(path)


def _find_member(zf: zipfile.ZipFile, wanted: str) -> str | None:
    """Locate a member case-insensitively, tolerating a wrapping directory."""
    target = wanted.lower()
    for member in zf.namelist():
        if member.lower() == target or member.lower().endswith("/" + target):
            return member
    return None


def _fallback_spec(zf: zipfile.ZipFile) -> CoreSpec:
    for candidate in ("taxon.txt", "taxon.tsv", "taxon.csv", "taxa.txt"):
        if member := _find_member(zf, candidate):
            log.warning("no usable meta.xml; falling back to %s with header row", member)
            delimiter = "," if member.lower().endswith(".csv") else "\t"
            return CoreSpec(
                location=member,
                delimiter=delimiter,
                quote='"',
                encoding="UTF-8",
                skip_header=1,
                columns={},  # filled from the header row
                id_index=None,
            )
    raise DwcaError(
        "archive has neither a readable meta.xml nor a taxon file; "
        f"members: {zf.namelist()[:20]}"
    )


def read_rows(path: Path) -> Iterator[SourceRow]:
    """Yield `SourceRow`s from a Darwin Core Archive."""
    with _open_archive(path) as zf:
        spec: CoreSpec
        if member := _find_member(zf, "meta.xml"):
            try:
                spec = _parse_meta(zf.read(member))
            except (ElementTree.ParseError, DwcaError) as exc:
                log.warning("meta.xml unusable (%s); falling back", exc)
                spec = _fallback_spec(zf)
        else:
            spec = _fallback_spec(zf)

        core_member = _find_member(zf, spec.location)
        if core_member is None:
            raise DwcaError(f"core file {spec.location!r} not found in archive")

        with zf.open(core_member) as raw:
            stream = io.TextIOWrapper(raw, encoding=spec.encoding, newline="")
            reader = csv.reader(
                stream,
                delimiter=spec.delimiter,
                quotechar=spec.quote or '"',
                # Publishers embed unescaped quotes in author strings often
                # enough that strict quoting loses real rows.
                quoting=csv.QUOTE_NONE if not spec.quote else csv.QUOTE_MINIMAL,
            )

            columns = dict(spec.columns)
            for line_no, values in enumerate(reader):
                if line_no < spec.skip_header:
                    if not columns:
                        columns = {i: _term(v.strip()) for i, v in enumerate(values)}
                    continue
                if not values:
                    continue
                yield _to_row(values, columns, spec.id_index)


def _to_row(
    values: list[str], columns: dict[int, str], id_index: int | None
) -> SourceRow:
    row = SourceRow(source_taxon_id=None, scientific_name="")
    classification: dict[str, str] = {}
    vernacular: list[dict[str, str]] = []
    raw: dict[str, str] = {}

    for index, value in enumerate(values):
        value = value.strip()
        if not value:
            continue
        term = columns.get(index)
        if not term:
            continue
        raw[term] = value

        if attr := TERM_MAP.get(term):
            # verbatimTaxonRank must not clobber a taxonRank already set.
            if attr == "rank" and row.rank:
                continue
            setattr(row, attr, value)
        elif term in CLASSIFICATION_RANKS:
            classification[term] = value
        elif term in VERNACULAR_TERMS:
            vernacular.append({"name": value, "language": None})

    # The <id> column is the record identifier when no taxonID term is mapped.
    if not row.source_taxon_id and id_index is not None and id_index < len(values):
        row.source_taxon_id = values[id_index].strip() or None

    row.classification = classification
    row.vernacular = vernacular
    row.raw = raw

    if not row.scientific_name:
        row.error = "no scientificName"
    return row
