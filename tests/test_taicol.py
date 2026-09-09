"""Reading a TaiCOL name export.

The fixtures are written inline rather than committed, so the conventions under
test — parallel comma-joined status/taxon lists, synonymy expressed through a
shared taxon_id, blank statuses — are readable next to the assertions.
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

from checklistdiff.ingest import taicol

COLUMNS = (
    "name_id", "simple_name", "name_author", "rank", "usage_status", "taxon_id",
    "kingdom", "phylum", "class", "order", "family", "genus",
    "common_name_c", "alternative_name_c", "nomenclature_name",
)

PLANT = {"kingdom": "Plantae", "family": "Testiaceae", "genus": "Testia"}

ROWS: list[dict[str, str]] = [
    # An accepted genus, and an accepted species inside it.
    {"name_id": "1", "simple_name": "Testia", "name_author": "Smith", "rank": "Genus",
     "usage_status": "accepted", "taxon_id": "t001",
     "kingdom": "Plantae", "family": "Testiaceae", "genus": "Testia"},
    {"name_id": "2", "simple_name": "Testia alpha", "name_author": "Smith",
     "rank": "Species", "usage_status": "accepted", "taxon_id": "t002",
     "common_name_c": "甲測試草", "alternative_name_c": "測試草,甲草", **PLANT},
    # A synonym: it names no accepted name, only the taxon it sits under.
    {"name_id": "3", "simple_name": "Testia vetus", "name_author": "Jones",
     "rank": "Species", "usage_status": "not-accepted", "taxon_id": "t002", **PLANT},
    # One name, two usages: accepted in its own taxon, misapplied in another.
    {"name_id": "4", "simple_name": "Testia beta", "name_author": "Brown",
     "rank": "Species", "usage_status": "accepted,misapplied",
     "taxon_id": "t004,t002", **PLANT},
    # Nomenclatural record with no taxonomic placement at all.
    {"name_id": "5", "simple_name": "Testia orphana", "name_author": "Grey",
     "rank": "Species", "usage_status": "", "taxon_id": ""},
    # Parallel lists of different lengths — unsplittable.
    {"name_id": "6", "simple_name": "Testia rotta", "name_author": "Noir",
     "rank": "Species", "usage_status": "accepted,misapplied",
     "taxon_id": "t006", **PLANT},
    # The same name_id and taxon_id repeated on two rows under two statuses,
    # rather than joined into one row.
    {"name_id": "7", "simple_name": "Testia gemina", "name_author": "Vert",
     "rank": "Species", "usage_status": "not-accepted", "taxon_id": "t002", **PLANT},
    {"name_id": "7", "simple_name": "Testia gemina", "name_author": "Vert",
     "rank": "Species", "usage_status": "misapplied", "taxon_id": "t002", **PLANT},
]


def _csv_text(rows: list[dict[str, str]] = ROWS) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(COLUMNS))
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in COLUMNS})
    return buf.getvalue()


def _write_csv(tmp_path: Path, rows: list[dict[str, str]] = ROWS) -> Path:
    path = tmp_path / "TaiCOL_name_20260625.csv"
    path.write_text(_csv_text(rows), encoding="utf-8")
    return path


def _write_zip(tmp_path: Path) -> Path:
    path = tmp_path / "TaiCOL_name_20260625.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("TaiCOL_name_20260625.csv", _csv_text())
    return path


def _by_anchor(path: Path) -> dict[str, object]:
    return {r.source_taxon_id: r for r in taicol.read_rows(path)}


class TestTaicolReader:
    def test_reads_zipped_and_bare_csv_alike(self, tmp_path: Path) -> None:
        """The zip holds a plain CSV, not a DwC-A, so both paths must work."""
        bare = [
            (r.source_taxon_id, r.full_name, r.status.value)
            for r in taicol.read_rows(_write_csv(tmp_path))
        ]
        zipped = [
            (r.source_taxon_id, r.full_name, r.status.value)
            for r in taicol.read_rows(_write_zip(tmp_path))
        ]
        assert bare == zipped

    def test_not_accepted_becomes_synonym(self, tmp_path: Path) -> None:
        """TaiCOL's own word for synonymy; unmapped it would land on UNKNOWN."""
        assert _by_anchor(_write_csv(tmp_path))["3"].status.value == "synonym"

    def test_synonym_points_at_the_accepted_name_id(self, tmp_path: Path) -> None:
        """The file links synonym to accepted only through a shared taxon_id."""
        rows = _by_anchor(_write_csv(tmp_path))
        assert rows["3"].accepted_taxon_id == "2"  # name_id accepted for t002

    def test_accepted_usage_gets_no_accepted_link(self, tmp_path: Path) -> None:
        rows = _by_anchor(_write_csv(tmp_path))
        assert rows["2"].accepted_taxon_id is None

    def test_multi_usage_row_splits_with_distinct_anchors(self, tmp_path: Path) -> None:
        """'accepted,misapplied' over 't004,t002' is two usages, not one."""
        rows = _by_anchor(_write_csv(tmp_path))
        assert rows["4"].status.value == "accepted"
        assert rows["4"].accepted_taxon_id is None
        # The extra usage keeps a deterministic key so it never collides with
        # the primary one in the loader's source-ID index.
        extra = rows["4#t002"]
        assert extra.status.value == "misapplied"
        assert extra.accepted_taxon_id == "2"

    def test_accepted_usage_anchors_regardless_of_list_order(
        self, tmp_path: Path
    ) -> None:
        """The bare name_id must follow the accepted usage, not position 0.

        TaiCOL does not keep the order of the comma-joined lists stable between
        releases. If the anchor tracked position, a name whose accepted usage
        moved from second to first would leave one anchor and arrive at
        another — reported as a removal plus an addition that never happened.
        """
        reordered = [
            r for r in ROWS if r["name_id"] != "4"
        ] + [
            {"name_id": "4", "simple_name": "Testia beta", "name_author": "Brown",
             "rank": "Species", "usage_status": "misapplied,accepted",
             "taxon_id": "t002,t004", **PLANT},
        ]
        rows = {
            r.source_taxon_id: r
            for r in taicol.read_rows(_write_csv(tmp_path, reordered))
        }
        # Same anchors as the accepted-first ordering in ROWS.
        assert rows["4"].status.value == "accepted"
        assert rows["4#t002"].status.value == "misapplied"

    def test_unplaced_name_is_kept_as_unknown(self, tmp_path: Path) -> None:
        """Dropping these would make the next release report them as removed."""
        row = _by_anchor(_write_csv(tmp_path))["5"]
        assert row.status.value == "unknown"
        assert row.accepted_taxon_id is None

    def test_mismatched_parallel_lists_are_reported_not_guessed(
        self, tmp_path: Path
    ) -> None:
        row = _by_anchor(_write_csv(tmp_path))["6"]
        assert row.error is not None
        assert "usage_status" in row.error

    def test_repeated_name_id_rows_get_distinct_anchors(self, tmp_path: Path) -> None:
        """`usage.source_taxon_id` is unique per release, so these must differ.

        Both rows share a name_id *and* a taxon_id, so disambiguating within a
        row is not enough — the reader has to track anchors across the file.
        """
        anchors = [
            r.source_taxon_id for r in taicol.read_rows(_write_csv(tmp_path))
            if not r.error
        ]
        assert len(anchors) == len(set(anchors))
        assert {"7", "7#t002"} <= set(anchors)

    def test_parent_comes_from_the_rank_above(self, tmp_path: Path) -> None:
        rows = _by_anchor(_write_csv(tmp_path))
        assert rows["2"].parent_taxon_id == "1"  # species -> genus Testia

    def test_genus_row_is_not_its_own_parent(self, tmp_path: Path) -> None:
        """The genus column repeats the row's own name at that rank.

        Testiaceae is not itself a row here, so the genus has no resolvable
        parent — what matters is that it does not point at itself.
        """
        assert _by_anchor(_write_csv(tmp_path))["1"].parent_taxon_id != "1"

    def test_synonyms_carry_no_parent(self, tmp_path: Path) -> None:
        """A synonym hangs off its accepted name, not off the classification."""
        assert _by_anchor(_write_csv(tmp_path))["3"].parent_taxon_id is None

    def test_links_are_never_emitted_as_name_strings(self, tmp_path: Path) -> None:
        """Both link kinds must be IDs, so the loader resolves them by lookup.

        A name-string link sends the loader into `NameParser.parse`, which spawns
        one gnparser process per name — at this file's scale that is the
        difference between a minutes-long ingest and an hours-long one.
        """
        for row in taicol.read_rows(_write_csv(tmp_path)):
            assert row.accepted_scientific_name is None
            assert row.parent_scientific_name is None

    def test_classification_and_vernacular_are_captured(self, tmp_path: Path) -> None:
        row = _by_anchor(_write_csv(tmp_path))["2"]
        assert row.classification == {
            "kingdom": "Plantae", "family": "Testiaceae", "genus": "Testia"
        }
        # alternative_name_c is itself a comma-joined list.
        assert [v["name"] for v in row.vernacular] == ["甲測試草", "測試草", "甲草"]
        assert {v["language"] for v in row.vernacular} == {"zh-TW"}
