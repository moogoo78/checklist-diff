from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from checklistdiff.api import app, describe_change
from checklistdiff.diff.engine import run_diff
from checklistdiff.ingest import csvmap
from checklistdiff.ingest.loader import ingest_release
from checklistdiff.models import Checklist, IdStability, SourceFormat

from tests.conftest import FIXTURES

pytestmark = pytest.mark.needs_gnparser


@pytest.fixture
def client(session):
    """A client bound to the per-test database.

    The app resolves its engine through `checklistdiff.db` on each request, and
    the `engine` fixture has already repointed that at a temp file, so no
    dependency override is needed.
    """
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
    session.commit()
    return TestClient(app)


class TestJsonApi:
    def test_healthz(self, client) -> None:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_checklists(self, client) -> None:
        data = client.get("/api/checklists").json()
        assert [c["code"] for c in data["checklists"]] == ["synth"]
        assert len(data["checklists"][0]["releases"]) == 2

    def test_trace_exact(self, client) -> None:
        data = client.get("/api/trace", params={"name": "Testia zeta Smith"}).json()
        assert data["match"] == "exact"
        assert data["count"] == 1

        appearances = data["traces"][0]["checklists"][0]["appearances"]
        assert [a["release"] for a in appearances] == ["v2023.1", "v2024.1"]
        assert appearances[1]["accepted_name"] == "Testia eta Smith"

    def test_trace_fuzzy_reports_its_uncertainty(self, client) -> None:
        data = client.get("/api/trace", params={"name": "Testia gama"}).json()
        assert data["match"] == "fuzzy"
        # A caller must be able to tell a guess from a fact.
        assert data["scores"]

    def test_trace_miss_is_not_an_error(self, client) -> None:
        r = client.get("/api/trace", params={"name": "Quercus glauca"})
        assert r.status_code == 200
        assert r.json()["count"] == 0

    def test_trace_requires_a_name(self, client) -> None:
        assert client.get("/api/trace").status_code == 422


class TestHtml:
    def test_index_lists_checklists(self, client) -> None:
        r = client.get("/")
        assert r.status_code == 200
        assert "synth" in r.text
        assert "Synthetic" in r.text

    def test_search_renders_a_timeline(self, client) -> None:
        r = client.get("/", params={"q": "Testia zeta"})
        assert r.status_code == 200
        assert "v2023.1" in r.text and "v2024.1" in r.text
        assert "accepted_changed" in r.text

    def test_fuzzy_results_are_labelled_as_such(self, client) -> None:
        r = client.get("/", params={"q": "Testia gama"})
        assert "not confirmed identities" in r.text
        assert "fuzzy" in r.text

    def test_advisory_change_is_visually_distinguished(self, client) -> None:
        # An inferred change must not render identically to an asserted one.
        r = client.get("/", params={"q": "Testia gammma"})
        assert "advisory" in r.text

    def test_miss_renders_cleanly(self, client) -> None:
        r = client.get("/", params={"q": "Quercus glauca"})
        assert r.status_code == 200
        assert "No match" in r.text

    def test_homonym_warning(self, client) -> None:
        r = client.get("/", params={"q": "Testia beta"})
        assert "homonyms" in r.text


class TestDescribeChange:
    @pytest.mark.parametrize(
        "kind,detail,expected",
        [
            ("status_changed", {"from_status": "accepted", "to_status": "synonym"},
             "accepted → synonym"),
            ("lumped", {"sunk": ["A", "B"], "into": "C"}, "A, B → C"),
            ("split", {"from": "A", "into": ["B", "C"]}, "A → B, C"),
            ("accepted_changed", {"from_accepted": None, "to_accepted": "X"}, "— → X"),
            ("id_replaced", {"from_id": "T1", "to_id": "T9"}, "T1 → T9"),
            ("added", {"name": "X"}, ""),
        ],
    )
    def test_phrases(self, kind, detail, expected) -> None:
        assert describe_change(detail, kind) == expected

    def test_missing_detail_does_not_crash(self) -> None:
        assert describe_change(None, "status_changed") == "None → None"
