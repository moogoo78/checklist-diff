"""Turn a name string typed by a human into `Name` rows.

Three tiers, tried in order, and the tier that succeeded is always reported back:
a user acting on a fuzzy match needs to know it was fuzzy.

    exact      the full string, including authorship, matched a norm_key
    canonical  the canonical name matched, ignoring authorship — may return
               several rows (homonyms, or the same name under different authors)
    fuzzy      FTS5 found candidates and rapidfuzz scored one above threshold

Note that a canonical hit returning several names is the normal, correct outcome
rather than an ambiguity to be resolved away: *Aotus* really is both a plant and
a monkey, and collapsing them would be the bug.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum

from rapidfuzz import fuzz
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from checklistdiff.config import get_settings
from checklistdiff.models import Name
from checklistdiff.naming.normalize import canonical_key, norm_key
from checklistdiff.naming.parse import GnparserError, NameParser

log = logging.getLogger(__name__)

# FTS5 treats these as syntax; a name typed with them would raise rather than
# return no results.
_FTS_UNSAFE = re.compile(r'["*():^-]')


class MatchTier(StrEnum):
    EXACT = "exact"
    CANONICAL = "canonical"
    FUZZY = "fuzzy"
    NONE = "none"


@dataclass
class Resolution:
    query: str
    tier: MatchTier
    names: list[Name]
    scores: dict[int, float] | None = None

    @property
    def found(self) -> bool:
        return bool(self.names)

    @property
    def ambiguous(self) -> bool:
        return len(self.names) > 1


def _parse(query: str) -> tuple[str, str] | None:
    """Return (norm_key, canonical_key) for a query, via gnparser."""
    try:
        parsed = NameParser().parse(query)
    except GnparserError as exc:
        # Resolution must degrade rather than fail: falling back to normalising
        # the raw string still resolves most well-formed queries.
        log.warning("gnparser unavailable (%s); normalising the raw string", exc)
        return norm_key(query, None), canonical_key(query)
    if parsed is None or not parsed.parsed_ok:
        return None
    return parsed.norm_key, parsed.canonical_key


def _fts_query(canonical: str) -> str | None:
    """Build a deliberately *loose* FTS5 query.

    Tokens are OR'd, not AND'd, and each gets a prefix wildcard. That matters:
    the whole point of the fuzzy tier is that a token is misspelled, and an AND
    query requires every token to match exactly — so "Testia gama" would find
    nothing, because no document contains "gama".

    FTS5 is only the coarse filter here. It casts a wide net ranked by bm25, and
    rapidfuzz does the actual discrimination on the candidates.
    """
    cleaned = _FTS_UNSAFE.sub(" ", canonical).strip()
    tokens = [t for t in cleaned.split() if t]
    if not tokens:
        return None
    return " OR ".join(f'"{tok}"*' for tok in tokens)


def resolve(
    session: Session, query: str, *, threshold: float | None = None, limit: int = 25
) -> Resolution:
    """Resolve a name string to `Name` rows, reporting how it was matched."""
    query = (query or "").strip()
    if not query:
        return Resolution(query=query, tier=MatchTier.NONE, names=[])

    keys = _parse(query)
    if keys is None:
        return Resolution(query=query, tier=MatchTier.NONE, names=[])
    nkey, ckey = keys

    # Tier 1 — exact, authorship included.
    if name := session.scalar(select(Name).where(Name.norm_key == nkey)):
        return Resolution(query=query, tier=MatchTier.EXACT, names=[name])

    # Tier 2 — canonical, ignoring authorship. Several hits is a real answer.
    canonical_hits = session.scalars(
        select(Name).where(Name.canonical_key == ckey).limit(limit)
    ).all()
    if canonical_hits:
        return Resolution(
            query=query, tier=MatchTier.CANONICAL, names=list(canonical_hits)
        )

    # Tier 3 — fuzzy. FTS5 narrows the field, rapidfuzz decides.
    cutoff = threshold if threshold is not None else get_settings().fuzzy_threshold
    match_expr = _fts_query(ckey)
    if not match_expr:
        return Resolution(query=query, tier=MatchTier.NONE, names=[])

    try:
        rows = session.execute(
            text(
                "SELECT rowid FROM name_fts WHERE name_fts MATCH :q "
                "ORDER BY rank LIMIT :n"
            ),
            {"q": match_expr, "n": limit * 4},
        ).all()
    except Exception as exc:  # noqa: BLE001 - a bad FTS query is a miss, not a crash
        log.warning("FTS query failed for %r: %s", query, exc)
        return Resolution(query=query, tier=MatchTier.NONE, names=[])

    if not rows:
        return Resolution(query=query, tier=MatchTier.NONE, names=[])

    candidates = session.scalars(
        select(Name).where(Name.id.in_([r[0] for r in rows]))
    ).all()

    scored = [
        (name, fuzz.ratio(ckey, name.canonical_key))
        for name in candidates
    ]
    hits = sorted(
        (pair for pair in scored if pair[1] >= cutoff),
        key=lambda pair: (-pair[1], pair[0].canonical_name),
    )[:limit]

    if not hits:
        return Resolution(query=query, tier=MatchTier.NONE, names=[])

    return Resolution(
        query=query,
        tier=MatchTier.FUZZY,
        names=[n for n, _ in hits],
        scores={n.id: round(float(s), 2) for n, s in hits},
    )
