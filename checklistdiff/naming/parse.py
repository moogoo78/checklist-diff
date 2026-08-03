"""gnparser client.

gnparser reads names on stdin and emits one JSON object per line, in input
order. Invoking it once per name would dominate ingest time, so names are sent in
batches and results are cached by verbatim string -- checklists repeat the same
name across releases constantly, so the cache hit rate is high in practice.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from checklistdiff.config import get_settings
from checklistdiff.models.enums import NomenclaturalCode
from checklistdiff.naming.normalize import canonical_key, norm_key

log = logging.getLogger(__name__)


class GnparserError(RuntimeError):
    pass


@dataclass(slots=True)
class ParsedName:
    """A parsed scientific name, ready to become a `Name` row."""

    verbatim: str
    canonical_name: str
    authorship: str | None
    rank: str | None
    rank_marker: str | None
    genus: str | None
    specific_epithet: str | None
    infraspecific_epithet: str | None
    nomenclatural_code: str
    norm_key: str
    canonical_key: str
    quality: int | None
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] | None = None

    @property
    def parsed_ok(self) -> bool:
        return bool(self.canonical_name)


def _components(details: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Pull genus / species / infraspecies out of gnparser's `details` block.

    The block is keyed by cardinality (`uninomial`, `species`, `infraspecies`),
    so the shape varies with the name; missing keys are normal, not errors.
    """
    genus = species = infra = None

    if uni := details.get("uninomial"):
        genus = uni.get("uninomial")
    if sp := details.get("species"):
        genus = sp.get("genus") or genus
        species = sp.get("species")
    if inf := details.get("infraspecies"):
        genus = inf.get("genus") or genus
        species = inf.get("species") or species
        entries = inf.get("infraspecies") or []
        if entries:
            # The final epithet is the one that names the taxon.
            infra = entries[-1].get("value")

    return genus, species, infra


def _infer_code(record: dict[str, Any]) -> str:
    """Guess the nomenclatural code from authorship shape.

    A trailing year in the authorship is a zoological convention
    ("Swinhoe, 1864"); botanical authorship omits it. This is a heuristic and is
    only ever a default -- an ingest source that states the code should override
    it.
    """
    authorship = record.get("authorship") or {}
    if authorship.get("year"):
        return NomenclaturalCode.ZOOLOGICAL.value
    return NomenclaturalCode.UNKNOWN.value


def _to_parsed(record: dict[str, Any]) -> ParsedName:
    verbatim = record.get("verbatim", "")
    canonical = record.get("canonical") or {}
    authorship = record.get("authorship") or {}
    details = record.get("details") or {}

    canonical_full = canonical.get("full") or ""
    author_norm = authorship.get("normalized") or None

    genus, species, infra = _components(details)

    warnings = [w.get("warning", "") for w in record.get("qualityWarnings") or []]
    if record.get("hybrid"):
        warnings.append(f"hybrid:{record['hybrid']}")
    if surrogate := record.get("surrogate"):
        # "Aster sp." / "Carex sp. 1" -- an open nomenclature placeholder, not a
        # name. Flagged so ingest can decide whether to keep it.
        warnings.append(f"surrogate:{surrogate}")

    return ParsedName(
        verbatim=verbatim,
        canonical_name=canonical_full,
        authorship=author_norm,
        rank=record.get("rank"),
        rank_marker=record.get("rank"),
        genus=genus,
        specific_epithet=species,
        infraspecific_epithet=infra,
        nomenclatural_code=_infer_code(record),
        norm_key=norm_key(canonical_full, author_norm),
        canonical_key=canonical_key(canonical_full),
        quality=record.get("quality"),
        warnings=[w for w in warnings if w],
        raw=record,
    )


def _run_gnparser(names: Sequence[str]) -> list[dict[str, Any]]:
    settings = get_settings()
    payload = "\n".join(n.replace("\n", " ") for n in names)

    try:
        proc = subprocess.run(
            [settings.gnparser_bin, "-f", "compact", "-j", "1", "-d"],
            input=payload,
            capture_output=True,
            text=True,
            check=True,
            timeout=max(60, len(names) // 100),
        )
    except FileNotFoundError as exc:
        raise GnparserError(
            f"gnparser binary not found at {settings.gnparser_bin!r}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise GnparserError(f"gnparser failed: {exc.stderr.strip()}") from exc

    records = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    if len(records) != len(names):
        raise GnparserError(
            f"gnparser returned {len(records)} records for {len(names)} names; "
            "the input/output alignment this code relies on has been broken"
        )
    return records


class NameParser:
    """Batching, caching front end to gnparser."""

    def __init__(self, batch_size: int | None = None) -> None:
        self._cache: dict[str, ParsedName] = {}
        self._batch_size = batch_size or get_settings().parse_batch_size

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    def parse_many(self, names: Iterable[str]) -> dict[str, ParsedName]:
        """Parse an iterable of verbatim names, returning {verbatim: ParsedName}.

        Duplicates and already-cached names cost nothing.
        """
        wanted = {n.strip() for n in names if n and n.strip()}
        missing = sorted(wanted - self._cache.keys())

        for start in range(0, len(missing), self._batch_size):
            chunk = missing[start : start + self._batch_size]
            log.debug("parsing %d names (%d cached)", len(chunk), len(self._cache))
            for record in _run_gnparser(chunk):
                parsed = _to_parsed(record)
                self._cache[parsed.verbatim] = parsed

        return {n: self._cache[n] for n in wanted if n in self._cache}

    def parse(self, name: str) -> ParsedName | None:
        return self.parse_many([name]).get(name.strip())
