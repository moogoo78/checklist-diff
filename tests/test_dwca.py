"""Darwin Core Archive reading.

Archives are built inline rather than committed as binaries, so the column
ordering and meta.xml quirks under test are visible in the test itself.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from checklistdiff.ingest import dwca
from checklistdiff.ingest.dwca import DwcaError

DWC = "http://rs.tdwg.org/dwc/terms"

# Deliberately not in the "obvious" order: meta.xml declares indices, and real
# publishers order columns however they like. A reader that assumes positions
# would pass on a tidy archive and corrupt a real one.
META = f"""<?xml version="1.0" encoding="UTF-8"?>
<archive xmlns="http://rs.tdwg.org/dwc/text/">
  <core encoding="UTF-8" fieldsTerminatedBy="\\t" linesTerminatedBy="\\n"
        fieldsEnclosedBy="" ignoreHeaderLines="1" rowType="{DWC}/Taxon">
    <files><location>taxon.txt</location></files>
    <id index="0"/>
    <field index="3" term="{DWC}/scientificName"/>
    <field index="0" term="{DWC}/taxonID"/>
    <field index="5" term="{DWC}/taxonomicStatus"/>
    <field index="1" term="{DWC}/family"/>
    <field index="4" term="{DWC}/scientificNameAuthorship"/>
    <field index="2" term="{DWC}/taxonRank"/>
    <field index="6" term="{DWC}/acceptedNameUsageID"/>
    <field index="7" term="{DWC}/vernacularName"/>
  </core>
</archive>
"""

CORE = "\n".join(
    [
        "taxonID\tfamily\ttaxonRank\tscientificName\tauthor\tstatus\tacceptedID\tvernacular",
        "T001\tTestiaceae\tspecies\tTestia alpha\tSmith\taccepted\t\talpha plant",
        "T002\tTestiaceae\tspecies\tTestia beta\tSmith\tsynonym\tT001\t",
    ]
)


def _archive(tmp_path: Path, *, meta: str | None = META, name: str = "a.zip") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        if meta is not None:
            zf.writestr("meta.xml", meta)
        zf.writestr("taxon.txt", CORE)
    return path


class TestDwca:
    def test_reads_columns_by_declared_index(self, tmp_path: Path) -> None:
        rows = list(dwca.read_rows(_archive(tmp_path)))
        assert len(rows) == 2

        alpha, beta = rows
        assert alpha.source_taxon_id == "T001"
        assert alpha.scientific_name == "Testia alpha"
        assert alpha.authorship == "Smith"
        assert alpha.rank == "species"
        assert alpha.status_raw == "accepted"
        assert alpha.classification == {"family": "Testiaceae"}
        assert alpha.vernacular == [{"name": "alpha plant", "language": None}]

        assert beta.accepted_taxon_id == "T001"
        assert beta.status_raw == "synonym"

    def test_nested_directory_is_tolerated(self, tmp_path: Path) -> None:
        # Archives exported from a folder often carry a wrapping directory.
        path = tmp_path / "nested.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("dwca-export/meta.xml", META)
            zf.writestr("dwca-export/taxon.txt", CORE)
        assert len(list(dwca.read_rows(path))) == 2

    def test_falls_back_to_header_row_without_meta(self, tmp_path: Path) -> None:
        rows = list(dwca.read_rows(_archive(tmp_path, meta=None)))
        assert len(rows) == 2
        # Header names are read as DwC terms; 'scientificName' maps, 'author'
        # does not (it is not a DwC term), so authorship stays unset here.
        assert rows[0].scientific_name == "Testia alpha"
        assert rows[0].source_taxon_id == "T001"

    def test_unparseable_meta_falls_back_rather_than_failing(
        self, tmp_path: Path
    ) -> None:
        rows = list(dwca.read_rows(_archive(tmp_path, meta="<archive><broken>")))
        assert len(rows) == 2

    def test_non_zip_is_rejected(self, tmp_path: Path) -> None:
        plain = tmp_path / "not.zip"
        plain.write_text("nope")
        with pytest.raises(DwcaError, match="not a zip"):
            list(dwca.read_rows(plain))

    def test_archive_without_a_taxon_file_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("readme.txt", "nothing here")
        with pytest.raises(DwcaError, match="neither a readable meta.xml"):
            list(dwca.read_rows(path))

    def test_rows_missing_a_name_are_flagged_not_dropped(self, tmp_path: Path) -> None:
        core = CORE + "\nT003\tTestiaceae\tspecies\t\t\taccepted\t\t"
        path = tmp_path / "b.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("meta.xml", META)
            zf.writestr("taxon.txt", core)
        rows = list(dwca.read_rows(path))
        assert rows[-1].error == "no scientificName"
