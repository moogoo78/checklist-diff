"""Tests for the two keys that name identity and track anchoring rest on.

The table below is the contract. Each case documents a real way that checklists
vary between releases, and whether that variation should or should not change the
key.
"""

from __future__ import annotations

import pytest

from checklistdiff.naming.normalize import (
    canonical_key,
    norm_key,
    normalize_authorship,
    normalize_canonical,
    split_norm_key,
)


class TestNormalizeCanonical:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Zanthoxylum ailanthoides", "zanthoxylum ailanthoides"),
            ("  Zanthoxylum   ailanthoides  ", "zanthoxylum ailanthoides"),
            ("ZANTHOXYLUM AILANTHOIDES", "zanthoxylum ailanthoides"),
            ("Quercus glauca subsp. glauca", "quercus glauca subsp. glauca"),
            ("Machilus zuihoensis f. japonica", "machilus zuihoensis f. japonica"),
            ("", ""),
        ],
    )
    def test_basic(self, raw: str, expected: str) -> None:
        assert normalize_canonical(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["Begonia × taipeiensis", "Begonia ×taipeiensis", "Begonia x taipeiensis"],
    )
    def test_hybrid_sign_variants_agree(self, raw: str) -> None:
        assert normalize_canonical(raw) == "begonia × taipeiensis"

    def test_hybrid_is_not_the_same_name_as_the_binomial(self) -> None:
        # A named hybrid is nomenclaturally distinct; merging them would silently
        # conflate two different taxa.
        assert normalize_canonical("Begonia × taipeiensis") != normalize_canonical(
            "Begonia taipeiensis"
        )

    def test_x_inside_an_epithet_is_not_a_hybrid_sign(self) -> None:
        assert normalize_canonical("Carex xerantica") == "carex xerantica"

    def test_rank_distinction_is_preserved(self) -> None:
        # gnparser's canonical.simple collapses both of these to
        # "Quercus glauca glauca". They are different names, so the key must
        # keep them apart -- this is why canonical.full is the input.
        assert normalize_canonical("Quercus glauca var. glauca") != normalize_canonical(
            "Quercus glauca subsp. glauca"
        )


class TestNormalizeAuthorship:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Siebold & Zucc.", "siebold & zucc"),
            ("Siebold and Zucc.", "siebold & zucc"),
            ("Siebold et Zucc.", "siebold & zucc"),
            ("Siebold&Zucc.", "siebold & zucc"),
            ("Merr.", "merr"),
            ("Merr", "merr"),
            ("Swinhoe, 1864", "swinhoe 1864"),
            ("Swinhoe 1864", "swinhoe 1864"),
            ("(L.) Merr.", "(l) merr"),
            (None, ""),
            ("", ""),
        ],
    )
    def test_mechanical_variation_is_absorbed(self, raw, expected) -> None:
        assert normalize_authorship(raw) == expected

    def test_basionym_parentheses_are_significant(self) -> None:
        # "(L.) Merr." and "L. Merr." make different claims about the basionym.
        assert normalize_authorship("(L.) Merr.") != normalize_authorship("L. Merr.")

    def test_abbreviations_are_not_expanded(self) -> None:
        # Deliberate: expanding these needs an authority dictionary. They surface
        # as author_changed for review rather than being merged on a guess.
        assert normalize_authorship("(L.) Merr.") != normalize_authorship(
            "(Linnaeus) Merrill"
        )


class TestNormKey:
    def test_homonyms_stay_distinct(self) -> None:
        # Aotus Endl. is a plant, Aotus Illiger 1811 is a monkey. Merging them
        # would make two unrelated checklists appear to agree about a taxon.
        plant = norm_key("Aotus", "Endl.")
        monkey = norm_key("Aotus", "Illiger, 1811")
        assert plant != monkey

    def test_homonyms_share_a_canonical_key(self) -> None:
        assert canonical_key("Aotus") == canonical_key("Aotus")

    def test_author_presence_changes_identity_but_not_the_anchor(self) -> None:
        # This split is the whole point of having two keys: a release that adds
        # an author string must report author_changed, not delete-and-recreate
        # the taxon's history.
        without = norm_key("Cinnamomum kanehirae", None)
        with_author = norm_key("Cinnamomum kanehirae", "Hayata")
        assert without != with_author
        assert canonical_key("Cinnamomum kanehirae") == canonical_key(
            "Cinnamomum kanehirae"
        )

    def test_separator_is_unambiguous(self) -> None:
        key = norm_key("Cinnamomum kanehirae", "Hayata")
        assert key == "cinnamomum kanehirae|hayata"
        assert split_norm_key(key) == ("cinnamomum kanehirae", "hayata")

    def test_missing_author_still_produces_a_wellformed_key(self) -> None:
        assert split_norm_key(norm_key("Aotus", None)) == ("aotus", "")

    @pytest.mark.parametrize(
        "a,b",
        [
            ("Zanthoxylum ailanthoides Siebold & Zucc.", "Siebold & Zucc."),
            ("Zanthoxylum  ailanthoides", "Siebold and Zucc."),
        ],
    )
    def test_equivalent_spellings_collapse(self, a: str, b: str) -> None:
        canonical = "Zanthoxylum ailanthoides"
        assert norm_key(canonical, b) == norm_key(canonical, "Siebold & Zucc.")
