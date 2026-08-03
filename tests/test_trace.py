from __future__ import annotations

import pytest
from sqlalchemy import select

from checklistdiff.diff.engine import run_diff
from checklistdiff.ingest import csvmap
from checklistdiff.ingest.loader import ingest_release
from checklistdiff.models import Checklist, IdStability, Name, SourceFormat
from checklistdiff.trace.resolve import MatchTier, resolve
from checklistdiff.trace.timeline import build_trace, trace_to_dict

from tests.conftest import FIXTURES

pytestmark = pytest.mark.needs_gnparser


@pytest.fixture
def loaded(session):
    checklist = Checklist(
        code="synth", title="Synthetic", id_stability=IdStability.PERSISTENT.value
    )
    session.add(checklist)
    session.flush()
    for version in ("v2023.1", "v2024.1"):
        ingest_release(
            session,
            checklist,
            version,
            csvmap.read_rows(FIXTURES / f"{version}.csv", FIXTURES / "map.yml"),
            source_format=SourceFormat.CSV.value,
        )
    run_diff(session, checklist, "v2023.1", "v2024.1")
    return session


class TestResolve:
    def test_exact_match_includes_authorship(self, loaded) -> None:
        r = resolve(loaded, "Testia zeta Smith")
        assert r.tier is MatchTier.EXACT
        assert len(r.names) == 1
        assert r.names[0].authorship == "Smith"

    def test_canonical_match_returns_every_author_variant(self, loaded) -> None:
        # Two names share the canonical "Testia beta" (Smith and Jones).
        # Returning both is the correct answer, not an ambiguity to resolve away.
        r = resolve(loaded, "Testia beta")
        assert r.tier is MatchTier.CANONICAL
        assert {n.authorship for n in r.names} == {"Smith", "Jones"}
        assert r.ambiguous

    def test_fuzzy_finds_a_misspelling(self, loaded) -> None:
        # The FTS filter must OR its tokens: an AND query cannot match "gama"
        # against "gamma", which is precisely the case fuzzy search exists for.
        r = resolve(loaded, "Testia gama")
        assert r.tier is MatchTier.FUZZY
        assert "Testia gamma" in {n.canonical_name for n in r.names}
        assert all(s >= 88 for s in r.scores.values())

    def test_fuzzy_reports_scores_so_callers_can_show_uncertainty(self, loaded) -> None:
        r = resolve(loaded, "Testia gama")
        assert r.scores
        assert set(r.scores) == {n.id for n in r.names}

    def test_no_match_is_a_clean_miss(self, loaded) -> None:
        r = resolve(loaded, "Quercus glauca")
        assert r.tier is MatchTier.NONE
        assert not r.found

    def test_empty_query(self, loaded) -> None:
        assert resolve(loaded, "   ").tier is MatchTier.NONE

    @pytest.mark.parametrize("query", ['Testia "zeta', "Testia (zeta)", "Testia*", "-"])
    def test_fts_metacharacters_do_not_crash(self, loaded, query: str) -> None:
        # A name typed with a quote or asterisk is FTS5 syntax; unescaped it
        # raises rather than returning no results.
        resolve(loaded, query)


class TestTimeline:
    def test_tracks_a_synonym_being_repointed(self, loaded) -> None:
        name = loaded.scalar(
            select(Name).where(Name.canonical_key == "testia zeta")
        )
        trace = build_trace(loaded, name)

        assert trace.checklist_count == 1
        cl = trace.checklists[0]
        assert [a.release_version for a in cl.appearances] == ["v2023.1", "v2024.1"]
        assert cl.appearances[0].accepted_name == "Testia epsilon Smith"
        assert cl.appearances[1].accepted_name == "Testia eta Smith"

        # The change is attached to the release it landed in.
        assert cl.appearances[0].changes == []
        assert {c["type"] for c in cl.appearances[1].changes} == {"accepted_changed"}

    def test_a_name_present_in_only_one_release(self, loaded) -> None:
        name = loaded.scalar(
            select(Name).where(Name.canonical_key == "testia kappa")
        )
        trace = build_trace(loaded, name)
        assert [a.release_version for a in trace.checklists[0].appearances] == [
            "v2023.1"
        ]

    def test_author_variants_are_surfaced_as_related(self, loaded) -> None:
        # Without this, a user searching one author string would wrongly conclude
        # the checklist does not have the name at all.
        name = loaded.scalar(
            select(Name).where(
                Name.canonical_key == "testia beta", Name.authorship == "Smith"
            )
        )
        trace = build_trace(loaded, name)
        assert [r["scientific_name"] for r in trace.related] == ["Testia beta Jones"]

    def test_advisory_changes_are_flagged_as_such(self, session) -> None:
        checklist = Checklist(
            code="s2", title="S", id_stability=IdStability.UNSTABLE.value
        )
        session.add(checklist)
        session.flush()
        for version in ("v2023.1", "v2024.1"):
            ingest_release(
                session,
                checklist,
                version,
                csvmap.read_rows(FIXTURES / f"{version}.csv", FIXTURES / "map.yml"),
                source_format=SourceFormat.CSV.value,
            )
        run_diff(session, checklist, "v2023.1", "v2024.1")

        name = session.scalar(
            select(Name).where(Name.canonical_key == "testia gammma")
        )
        trace = build_trace(session, name)
        changes = trace.checklists[0].appearances[0].changes
        by_type = {c["type"]: c for c in changes}
        assert by_type["probable_rename"]["advisory"] is True
        assert by_type["added"]["advisory"] is False

    def test_serialises_to_json_safe_types(self, loaded) -> None:
        import json

        name = loaded.scalar(select(Name).where(Name.canonical_key == "testia zeta"))
        payload = trace_to_dict(build_trace(loaded, name))
        json.dumps(payload)  # must not raise on dates or enums

        assert payload["checklists"][0]["appearances"][0]["released_on"] is None or isinstance(
            payload["checklists"][0]["appearances"][0]["released_on"], str
        )
