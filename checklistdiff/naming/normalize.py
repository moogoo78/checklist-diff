"""Derivation of the two keys that everything else depends on.

This module is the most safety-critical code in the project. Two keys are
derived, and the split between them is deliberate:

`canonical_key` — the canonical name alone, normalised.
    Groups homonyms together. Used to anchor tracks when a checklist's own IDs
    are not trustworthy.

`norm_key` — canonical_key plus normalised authorship.
    Identity of a `Name` row. Authorship must be included, or the plant genus
    *Aotus* Endl. and the monkey genus *Aotus* Illiger, 1811 would collapse into
    one row and every checklist referencing either would appear to agree.

Why two rather than one: under name anchoring, a release that adds or corrects an
author string would fragment the track if the anchor included authorship — the
taxon would look deleted and re-added. Anchoring on `canonical_key` instead keeps
the thread intact and lets the diff engine report an honest `author_changed`.

The failure modes are asymmetric and both bad:
  too loose  -> two genuinely different names merge, and the merged history is
                silently wrong
  too strict -> one name fragments across releases, filling the change log with
                phantom additions and removals
When in doubt, prefer too strict: a spurious change is visible and reviewable,
whereas a bad merge is invisible.
"""

from __future__ import annotations

import re
import unicodedata

# gnparser emits U+00D7; sources variously use 'x', 'X' or the letter chi.
HYBRID_SIGNS = ("×", "✕", "⨯")

_WS = re.compile(r"\s+")
_HYBRID_LOOSE = re.compile(r"(?<![a-z])[xX](?=\s)")


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def normalize_canonical(canonical: str) -> str:
    """Normalise a canonical name into a comparison key.

    Expects gnparser's `canonical.full`, which has already unified infraspecific
    markers (`ssp.` -> `subsp.`) while preserving the rank distinction that
    matters -- `var. glauca` and `subsp. glauca` are different names and must not
    share a key. (`canonical.simple` collapses both to the same string, which is
    why it is not used here.)
    """
    if not canonical:
        return ""

    text = _nfc(canonical).strip()

    # Unify hybrid signs, then normalise spacing so 'Begonia ×taipeiensis' and
    # 'Begonia × taipeiensis' agree. The sign is preserved, not stripped: a named
    # hybrid is a distinct name from the non-hybrid binomial.
    for sign in HYBRID_SIGNS:
        text = text.replace(sign, "×")
    text = _HYBRID_LOOSE.sub("×", text)
    text = re.sub(r"×\s*", "× ", text)

    text = _WS.sub(" ", text).strip().lower()
    return text


def normalize_authorship(authorship: str | None) -> str:
    """Normalise an authorship string.

    Handles only mechanical variation -- case, spacing, punctuation, `and` vs
    `&`. Deliberately does *not* expand abbreviations: mapping `(L.) Merr.` onto
    `(Linnaeus) Merrill` needs an authority dictionary (IPNI/Harvard), and
    guessing at it would merge names that a curator has not agreed are the same.
    Such pairs surface as `author_changed` for review, which is the honest
    outcome.

    Parentheses are preserved -- `(L.) Merr.` and `L. Merr.` make different
    nomenclatural claims about the basionym.
    """
    if not authorship:
        return ""

    text = _nfc(authorship).strip().lower()
    text = text.replace(" and ", " & ")
    text = text.replace(" et ", " & ")

    # Abbreviation dots carry no information once the string is lowercased, and
    # dropping them makes 'Merr.' and 'Merr' agree.
    text = text.replace(".", " ")

    text = re.sub(r"\s*&\s*", " & ", text)
    text = re.sub(r"\s*,\s*", " ", text)
    text = re.sub(r"\(\s*", "(", text)
    text = re.sub(r"\s*\)", ")", text)
    text = _WS.sub(" ", text).strip()
    return text


def canonical_key(canonical: str) -> str:
    """Track-anchoring key: the canonical name, ignoring authorship."""
    return normalize_canonical(canonical)


def norm_key(canonical: str, authorship: str | None) -> str:
    """Identity key for a `Name` row: canonical plus authorship.

    The two parts are joined by '|', which cannot occur in either component, so
    the key is unambiguous even when authorship is absent.
    """
    return f"{normalize_canonical(canonical)}|{normalize_authorship(authorship)}"


def split_norm_key(key: str) -> tuple[str, str]:
    """Inverse of `norm_key`, for reporting."""
    canonical, _, authorship = key.partition("|")
    return canonical, authorship
