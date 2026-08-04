from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from checklistdiff.ingest import csvmap
from checklistdiff.ingest.csvmap import FieldMap, FieldMapError
from checklistdiff.ingest.loader import IngestError, ingest_release
from checklistdiff.ingest.rows import SourceRow, normalize_status
from checklistdiff.models import Name, Release, SourceFormat, TaxonomicStatus, Usage

from tests.conftest import FIXTURES

pytestmark = pytest.mark.needs_gnparser


def _load(session, checklist, version: str, **kw):
    rows = csvmap.read_rows(FIXTURES / f"{version}.csv", FIXTURES / "map.yml")
    return ingest_release(
        session,
        checklist,
        version,
        rows,
        source_format=SourceFormat.CSV.value,
        source_sha256=f"sha-{version}",
        **kw,
    )


class TestStatusNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("accepted", TaxonomicStatus.ACCEPTED),
            ("Accepted", TaxonomicStatus.ACCEPTED),
            ("accepted name", TaxonomicStatus.ACCEPTED),
            ("valid", TaxonomicStatus.ACCEPTED),
            ("synonym", TaxonomicStatus.SYNONYM),
            ("  SYNONYM  ", TaxonomicStatus.SYNONYM),
            ("homotypic synonym", TaxonomicStatus.SYNONYM),
            ("misapplied", TaxonomicStatus.MISAPPLIED),
            ("", TaxonomicStatus.UNKNOWN),
            (None, TaxonomicStatus.UNKNOWN),
        ],
    )
    def test_vocabulary(self, raw, expected) -> None:
        assert normalize_status(raw) is expected

    def test_unrecognised_is_unknown_not_guessed(self) -> None:
        # Guessing a status silently changes what the diff reports about a taxon.
        assert normalize_status("something bespoke") is TaxonomicStatus.UNKNOWN


class TestFieldMap:
    def test_scientific_name_is_required(self, tmp_path: Path) -> None:
        p = tmp_path / "m.yml"
        p.write_text("fields:\n  rank: Rank\n")
        with pytest.raises(FieldMapError, match="scientific_name"):
            FieldMap.from_yaml(p)

    def test_unknown_field_is_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "m.yml"
        p.write_text("fields:\n  scientific_name: N\n  bogus: B\n")
        with pytest.raises(FieldMapError, match="bogus"):
            FieldMap.from_yaml(p)

    def test_missing_column_fails_loudly(self, tmp_path: Path) -> None:
        # A renamed column must not silently yield NULLs; that would make the
        # next diff report the whole checklist as changed.
        csv_path = tmp_path / "d.csv"
        csv_path.write_text("name,rank\nTestia alpha,species\n")
        map_path = tmp_path / "m.yml"
        map_path.write_text("fields:\n  scientific_name: scientificName\n")
        with pytest.raises(FieldMapError, match="not in file"):
            list(csvmap.read_rows(csv_path, map_path))


class TestIngest:
    def test_loads_the_fixture(self, session, checklist) -> None:
        report = _load(session, checklist, "v2023.1")

        assert report.rows_read == 16
        assert report.usages_written == 16
        assert not report.skipped

        release = session.get(Release, report.release_id)
        assert release.usage_count == 16
        assert release.status == "ready"

    def test_names_are_deduped_across_releases(self, session, checklist) -> None:
        first = _load(session, checklist, "v2023.1")
        second = _load(session, checklist, "v2024.1")

        # The second release reuses most names rather than creating duplicates —
        # this dedup is what makes cross-release and cross-checklist comparison
        # a plain join.
        assert second.names_reused > 10
        assert first.names_created > second.names_created

        total_usages = session.scalar(select(func.count()).select_from(Usage))
        total_names = session.scalar(select(func.count()).select_from(Name))
        assert total_usages == 33
        assert total_names < total_usages

    def test_author_change_creates_a_distinct_name_row(self, session, checklist) -> None:
        _load(session, checklist, "v2023.1")
        _load(session, checklist, "v2024.1")

        # T002: 'Testia beta Smith' -> 'Testia beta Jones'. Two Name rows (author
        # is part of identity) sharing one canonical_key (so the track survives).
        betas = session.scalars(
            select(Name).where(Name.canonical_key == "testia beta")
        ).all()
        assert len(betas) == 2
        assert {n.authorship for n in betas} == {"Smith", "Jones"}
        assert len({n.canonical_key for n in betas}) == 1

    def test_synonym_links_resolve_within_the_release(self, session, checklist) -> None:
        report = _load(session, checklist, "v2023.1")
        assert not report.unresolved_accepted

        zeta = self._usage(session, report.release_id, "T006")
        assert zeta.status == TaxonomicStatus.SYNONYM.value
        assert zeta.accepted_usage_id is not None

        epsilon = self._usage(session, report.release_id, "T005")
        assert zeta.accepted_usage_id == epsilon.id
        # The accepted target must live in the same release, always.
        assert epsilon.release_id == report.release_id

    def test_parent_links_resolve(self, session, checklist) -> None:
        report = _load(session, checklist, "v2023.1")
        assert not report.unresolved_parent

        theta = self._usage(session, report.release_id, "T008")
        genus = self._usage(session, report.release_id, "G001")
        assert theta.parent_usage_id == genus.id

    def test_classification_and_vernacular_are_captured(self, session, checklist) -> None:
        report = _load(session, checklist, "v2023.1")
        alpha = self._usage(session, report.release_id, "T001")
        assert alpha.classification == {"family": "Testiaceae", "genus": "Testia"}
        assert alpha.vernacular == [{"name": "alpha test-plant", "language": "en"}]

    def test_reingest_is_refused_by_default(self, session, checklist) -> None:
        _load(session, checklist, "v2023.1")
        with pytest.raises(IngestError, match="already ingested"):
            _load(session, checklist, "v2023.1")

    def test_replace_overwrites(self, session, checklist) -> None:
        _load(session, checklist, "v2023.1")
        second = _load(session, checklist, "v2023.1", replace=True)

        # Exactly one release survives, and the previous release's usages are
        # deleted rather than left orphaned. (Note SQLite may reuse the freed
        # rowid, so the new release_id is not necessarily different.)
        releases = session.scalars(
            select(Release).where(Release.version == "v2023.1")
        ).all()
        assert len(releases) == 1
        assert releases[0].id == second.release_id

        assert session.scalar(select(func.count()).select_from(Usage)) == 16
        assert (
            session.scalar(
                select(func.count())
                .select_from(Usage)
                .where(Usage.release_id != second.release_id)
            )
            == 0
        )
        # Names outlive the release they arrived with — they are shared, not owned.
        assert second.names_reused == 16 and second.names_created == 0

    def test_rows_without_a_name_are_skipped_not_fatal(self, session, checklist) -> None:
        rows = [
            SourceRow(source_taxon_id="A", scientific_name="Testia alpha", authorship="Smith"),
            SourceRow(source_taxon_id="B", scientific_name=""),
            SourceRow(source_taxon_id="C", scientific_name="Testia beta", authorship="Smith"),
        ]
        report = ingest_release(
            session, checklist, "partial", rows, source_format=SourceFormat.CSV.value
        )
        assert report.rows_read == 3
        assert report.usages_written == 2
        assert len(report.skipped) == 1

    @staticmethod
    def _usage(session, release_id: int, taxon_id: str) -> Usage:
        return session.scalar(
            select(Usage).where(
                Usage.release_id == release_id, Usage.source_taxon_id == taxon_id
            )
        )


class TestLinkResolutionBatching:
    """Name-based links must not cost one gnparser process each.

    `_run_gnparser` is a subprocess spawn. Resolving links one at a time is
    correct but turns a minutes-long ingest into an hours-long one on a real
    checklist, and nothing about the result changes — so only a count can catch
    a regression here.
    """

    @staticmethod
    def _count_spawns(monkeypatch) -> list[int]:
        from checklistdiff.naming import parse as parse_mod

        spawns: list[int] = []
        original = parse_mod._run_gnparser

        def counting(names):
            names = list(names)
            spawns.append(len(names))
            return original(names)

        monkeypatch.setattr(parse_mod, "_run_gnparser", counting)
        return spawns

    @staticmethod
    def _rows(count: int) -> list[SourceRow]:
        accepted = [
            SourceRow(
                source_taxon_id=None,
                scientific_name=f"Testia alpha{i}",
                authorship="Smith",
                status_raw="accepted",
            )
            for i in range(count)
        ]
        # No taxon IDs anywhere, so every synonym link has to go through the
        # parser rather than the source-ID index.
        synonyms = [
            SourceRow(
                source_taxon_id=None,
                scientific_name=f"Testia vetus{i}",
                authorship="Jones",
                status_raw="synonym",
                accepted_scientific_name=f"Testia alpha{i} Smith",
            )
            for i in range(count)
        ]
        return accepted + synonyms

    def test_spawn_count_does_not_grow_with_link_count(
        self, session, checklist, monkeypatch
    ) -> None:
        spawns = self._count_spawns(monkeypatch)

        ingest_release(
            session,
            checklist,
            "batched",
            self._rows(40),
            source_format=SourceFormat.CSV.value,
        )

        # One batch covers every name in the file. The link pass adds nothing:
        # it shares the parser, so the names it looks up are already cached.
        assert len(spawns) == 1, f"expected 1 gnparser spawn, got {len(spawns)}"

    def test_links_still_resolve_when_batched(self, session, checklist) -> None:
        report = ingest_release(
            session,
            checklist,
            "resolved",
            self._rows(40),
            source_format=SourceFormat.CSV.value,
        )
        assert report.accepted_resolved == 40
        assert report.unresolved_accepted == []
