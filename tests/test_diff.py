"""The diff engine, verified against the synthetic fixture.

Read `tests/fixtures/synthetic/README.md` first — it is the specification, and
this file asserts it.

Everything runs twice, under both anchoring modes. They must produce *different*
results: a change that makes them agree is a bug, because the two modes are
answering different questions about a checklist whose IDs may or may not mean
anything.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from checklistdiff.diff.engine import DiffError, run_diff
from checklistdiff.ingest import csvmap
from checklistdiff.ingest.loader import ingest_release
from checklistdiff.models import (
    Change,
    ChangeType,
    Checklist,
    IdStability,
    SourceFormat,
    Track,
    UsageTrack,
)

from tests.conftest import FIXTURES

pytestmark = pytest.mark.needs_gnparser


def _ingest_both(session, checklist) -> None:
    for version in ("v2023.1", "v2024.1"):
        ingest_release(
            session,
            checklist,
            version,
            csvmap.read_rows(FIXTURES / f"{version}.csv", FIXTURES / "map.yml"),
            source_format=SourceFormat.CSV.value,
        )


@pytest.fixture
def diffed(request, session):
    """Ingest the fixture and diff it under the requested anchoring mode."""
    stability = getattr(request, "param", IdStability.PERSISTENT.value)
    checklist = Checklist(code="synth", title="Synthetic", id_stability=stability)
    session.add(checklist)
    session.flush()

    _ingest_both(session, checklist)
    report = run_diff(session, checklist, "v2023.1", "v2024.1")
    return session, checklist, report


def changes_of(session, kind: ChangeType) -> list[Change]:
    return list(
        session.scalars(select(Change).where(Change.type == kind.value)).all()
    )


def names_in(changes: list[Change], key: str = "name") -> set[str]:
    return {c.detail.get(key) for c in changes if c.detail}


# ---------------------------------------------------------------------------
# ID anchoring — the publisher's taxonID is trustworthy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("diffed", [IdStability.PERSISTENT.value], indirect=True)
class TestPersistentAnchoring:
    def test_anchor_choice(self, diffed) -> None:
        _, _, report = diffed
        assert report.anchor_kind == "source_id"

    def test_unchanged_taxon_produces_no_change(self, diffed) -> None:
        session, _, _ = diffed
        # T001 Testia alpha is identical in both releases.
        for kind in ChangeType:
            for change in changes_of(session, kind):
                assert (change.detail or {}).get("name") != "Testia alpha"

    def test_author_changed(self, diffed) -> None:
        session, _, _ = diffed
        found = changes_of(session, ChangeType.AUTHOR_CHANGED)
        assert len(found) == 1
        assert found[0].detail["name"] == "Testia beta"
        assert found[0].detail["from_author"] == "Smith"
        assert found[0].detail["to_author"] == "Jones"

    def test_renamed(self, diffed) -> None:
        # Only detectable when the ID carries the identity across the spelling fix.
        session, _, _ = diffed
        found = changes_of(session, ChangeType.RENAMED)
        assert len(found) == 1
        assert found[0].detail["from_name"] == "Testia gamma"
        assert found[0].detail["to_name"] == "Testia gammma"

    def test_status_changed(self, diffed) -> None:
        session, _, _ = diffed
        found = changes_of(session, ChangeType.STATUS_CHANGED)
        # T004 accepted->synonym, T013 accepted->synonym, T015/T016 synonym->accepted
        assert names_in(found) == {
            "Testia delta",
            "Testia nu",
            "Testia omicron",
            "Testia pi",
        }

    def test_accepted_changed_reports_the_repointed_synonym(self, diffed) -> None:
        session, _, _ = diffed
        found = changes_of(session, ChangeType.ACCEPTED_CHANGED)
        by_name = {c.detail["name"]: c.detail for c in found}

        # T006 zeta: synonym of epsilon -> synonym of eta. The change users most
        # need to see, because it silently re-identifies their records.
        assert by_name["Testia zeta"]["from_accepted"] == "Testia epsilon"
        assert by_name["Testia zeta"]["to_accepted"] == "Testia eta"

        # T004 delta gained an accepted pointer when it was sunk.
        assert by_name["Testia delta"]["from_accepted"] is None
        assert by_name["Testia delta"]["to_accepted"] == "Testia epsilon"

    def test_reclassified(self, diffed) -> None:
        session, _, _ = diffed
        found = changes_of(session, ChangeType.RECLASSIFIED)
        assert len(found) == 1
        assert found[0].detail["name"] == "Testia theta"
        assert found[0].detail["from_parent"] == "Testia"
        assert found[0].detail["to_parent"] == "Testiella"

    def test_rank_changed(self, diffed) -> None:
        session, _, _ = diffed
        found = changes_of(session, ChangeType.RANK_CHANGED)
        assert len(found) == 1
        assert found[0].detail["name"] == "Testia iota"
        assert (found[0].detail["from_rank"], found[0].detail["to_rank"]) == (
            "species",
            "subspecies",
        )

    def test_added_and_removed(self, diffed) -> None:
        session, _, _ = diffed
        assert names_in(changes_of(session, ChangeType.ADDED)) == {
            "Testia lambda",
            "Testiella",
        }
        assert names_in(changes_of(session, ChangeType.REMOVED)) == {"Testia kappa"}

    def test_id_replaced_suppresses_the_phantom_add_and_remove(self, diffed) -> None:
        session, _, _ = diffed
        found = changes_of(session, ChangeType.ID_REPLACED)
        assert len(found) == 1
        assert found[0].detail["name"] == "Testia mu"
        assert (found[0].detail["from_id"], found[0].detail["to_id"]) == ("T012", "T912")

        # The renumbering must not also appear as an addition and a removal.
        assert "Testia mu" not in names_in(changes_of(session, ChangeType.ADDED))
        assert "Testia mu" not in names_in(changes_of(session, ChangeType.REMOVED))

    def test_id_replacement_is_not_also_guessed_at(self, diffed) -> None:
        # A settled explanation must not compete with a fuzzy one: the renumbered
        # T012 -> T912 pair once produced a bogus 100%-confidence
        # "rename of Testia mu to Testia mu" alongside the correct id_replaced.
        session, _, _ = diffed
        guesses = changes_of(session, ChangeType.PROBABLE_RENAME)
        assert guesses == []

    def test_no_change_claims_a_name_was_renamed_to_itself(self, diffed) -> None:
        session, _, _ = diffed
        for kind in (ChangeType.RENAMED, ChangeType.PROBABLE_RENAME):
            for change in changes_of(session, kind):
                d = change.detail or {}
                assert d.get("from_name") != d.get("to_name")

    def test_lumped(self, diffed) -> None:
        session, _, _ = diffed
        found = changes_of(session, ChangeType.LUMPED)
        assert len(found) == 1
        detail = found[0].detail
        assert detail["into"] == "Testia epsilon"
        assert set(detail["sunk"]) == {"Testia delta", "Testia nu"}
        assert detail["count"] == 3  # delta, nu, and epsilon itself

    def test_split(self, diffed) -> None:
        session, _, _ = diffed
        found = changes_of(session, ChangeType.SPLIT)
        assert len(found) == 1
        detail = found[0].detail
        assert detail["from"] == "Testia xi"
        assert set(detail["into"]) == {"Testia xi", "Testia omicron", "Testia pi"}
        assert detail["count"] == 3

    def test_set_level_changes_carry_no_single_track(self, diffed) -> None:
        session, _, _ = diffed
        for kind in (ChangeType.LUMPED, ChangeType.SPLIT):
            for change in changes_of(session, kind):
                assert change.track_id is None

    def test_tracks_are_materialised(self, diffed) -> None:
        session, _, _ = diffed
        tracks = session.scalars(select(Track)).all()
        assert all(t.anchor_kind == "source_id" for t in tracks)
        # Every usage in both releases belongs to exactly one track.
        memberships = session.scalar(select(func.count()).select_from(UsageTrack))
        assert memberships == 16 + 17


# ---------------------------------------------------------------------------
# Name anchoring — the publisher's IDs shuffle between exports
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("diffed", [IdStability.UNSTABLE.value], indirect=True)
class TestNameAnchoring:
    def test_anchor_choice(self, diffed) -> None:
        _, _, report = diffed
        assert report.anchor_kind == "name"

    def test_renumbering_is_invisible(self, diffed) -> None:
        # T012 -> T912 is the same name; under name anchoring nothing happened,
        # which is the correct answer.
        session, _, _ = diffed
        assert not changes_of(session, ChangeType.ID_REPLACED)
        assert "Testia mu" not in names_in(changes_of(session, ChangeType.ADDED))
        assert "Testia mu" not in names_in(changes_of(session, ChangeType.REMOVED))

    def test_rename_cannot_be_asserted(self, diffed) -> None:
        # The canonical name IS the anchor, so a spelling change breaks the
        # thread by construction. No `renamed` row is possible.
        session, _, _ = diffed
        assert not changes_of(session, ChangeType.RENAMED)

    def test_rename_surfaces_as_an_advisory_suggestion(self, diffed) -> None:
        session, _, _ = diffed
        found = changes_of(session, ChangeType.PROBABLE_RENAME)
        assert len(found) == 1
        detail = found[0].detail
        assert detail["from_name"] == "Testia gamma"
        assert detail["to_name"] == "Testia gammma"
        assert detail["advisory"] is True
        # Scored, not asserted.
        assert 88 <= found[0].confidence <= 100

    def test_advisory_rename_does_not_suppress_the_add_and_remove(
        self, diffed
    ) -> None:
        # A fuzzy guess must never erase the underlying facts; a curator has to
        # be able to see that a name left and another arrived.
        session, _, _ = diffed
        assert "Testia gammma" in names_in(changes_of(session, ChangeType.ADDED))
        assert "Testia gamma" in names_in(changes_of(session, ChangeType.REMOVED))

    def test_author_change_still_detected(self, diffed) -> None:
        # The payoff of anchoring on canonical_key rather than norm_key: the
        # thread survives an author correction, so it is reported as a change
        # rather than shredded into a delete and an add.
        session, _, _ = diffed
        found = changes_of(session, ChangeType.AUTHOR_CHANGED)
        assert len(found) == 1
        assert found[0].detail["name"] == "Testia beta"

    def test_taxonomic_acts_are_still_detected(self, diffed) -> None:
        # Lumps, splits and repointings do not depend on the anchor.
        session, _, _ = diffed
        assert len(changes_of(session, ChangeType.LUMPED)) == 1
        assert len(changes_of(session, ChangeType.SPLIT)) == 1
        assert "Testia zeta" in names_in(changes_of(session, ChangeType.ACCEPTED_CHANGED))

    def test_tracks_use_the_name_anchor(self, diffed) -> None:
        session, _, _ = diffed
        tracks = session.scalars(select(Track)).all()
        assert all(t.anchor_kind == "name" for t in tracks)


# ---------------------------------------------------------------------------
# Engine behaviour
# ---------------------------------------------------------------------------

class TestEngine:
    def test_rerun_is_refused_without_force(self, session) -> None:
        checklist = Checklist(code="synth", title="S")
        session.add(checklist)
        session.flush()
        _ingest_both(session, checklist)

        run_diff(session, checklist, "v2023.1", "v2024.1")
        with pytest.raises(DiffError, match="already has"):
            run_diff(session, checklist, "v2023.1", "v2024.1")

    def test_force_rebuilds_rather_than_duplicating(self, session) -> None:
        checklist = Checklist(code="synth", title="S")
        session.add(checklist)
        session.flush()
        _ingest_both(session, checklist)

        first = run_diff(session, checklist, "v2023.1", "v2024.1")
        second = run_diff(session, checklist, "v2023.1", "v2024.1", force=True)

        assert first.counts == second.counts
        assert session.scalar(select(func.count()).select_from(Change)) == second.total

    def test_unknown_release_is_reported_clearly(self, session) -> None:
        checklist = Checklist(code="synth", title="S")
        session.add(checklist)
        session.flush()
        _ingest_both(session, checklist)

        with pytest.raises(DiffError, match="no release 'v9999'"):
            run_diff(session, checklist, "v2023.1", "v9999")

    def test_self_diff_is_refused(self, session) -> None:
        checklist = Checklist(code="synth", title="S")
        session.add(checklist)
        session.flush()
        _ingest_both(session, checklist)

        with pytest.raises(DiffError, match="against itself"):
            run_diff(session, checklist, "v2023.1", "v2023.1")


class TestAnchoringModesDisagree:
    """The two modes must not converge — that is the point of having both."""

    def test_they_produce_different_change_sets(self, session) -> None:
        results = {}
        for stability in (IdStability.PERSISTENT.value, IdStability.UNSTABLE.value):
            checklist = Checklist(
                code=f"synth-{stability}", title="S", id_stability=stability
            )
            session.add(checklist)
            session.flush()
            _ingest_both(session, checklist)
            results[stability] = run_diff(
                session, checklist, "v2023.1", "v2024.1", force=True
            ).counts

        persistent = results[IdStability.PERSISTENT.value]
        unstable = results[IdStability.UNSTABLE.value]

        assert persistent != unstable
        assert persistent[ChangeType.RENAMED.value] == 1
        assert unstable[ChangeType.RENAMED.value] == 0
        assert persistent[ChangeType.ID_REPLACED.value] == 1
        assert unstable[ChangeType.ID_REPLACED.value] == 0

        # Fuzzy guessing is the *fallback* for what ID anchoring gets for free:
        # it should fire only where the certain mechanism is unavailable.
        assert unstable[ChangeType.PROBABLE_RENAME.value] == 1
        assert persistent[ChangeType.PROBABLE_RENAME.value] == 0
