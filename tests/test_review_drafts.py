"""AI drafts retain original-document provenance without pretending to quote it."""

from __future__ import annotations

import copy

import pytest

from src.review_documents import source_pages
from src.review_store import ReviewError, ReviewStore


CONTENT = b"Client contact notes\nPhone: 503-555-0142\n"


def field(detail, identifier):
    return next(item for item in detail["fields"] if item["id"] == identifier)


@pytest.fixture
def store(tmp_path):
    return ReviewStore(tmp_path / "workspace")


@pytest.fixture
def detail(store):
    return store.create_return(2025, client_name="Avery Morgan", return_type="individual")


def import_values(store, detail, values, content=CONTENT):
    with store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = store._load(connection, detail["id"])
        store.import_extracted_group(
            connection,
            current,
            filename="contact.txt",
            content=content,
            media_type="text/plain",
            values=values,
            pages=source_pages(content, "text/plain"),
        )
        connection.commit()
    return store.get_return(detail["id"])


def literal_phone(value="503-555-0142"):
    return {
        "fieldId": "phone",
        "value": value,
        "evidence": {
            "page": 1,
            "lineStart": 2,
            "lineEnd": 2,
            "quote": "Phone: 503-555-0142",
        },
    }


def test_archived_ai_drafts_reference_only_their_original(store, detail):
    notes = "Summary drawn from the complete intake document. " * 30
    extracted = [
        {"fieldId": "full_name", "value": "Avery Morgan"},
        {"fieldId": "tax_year", "value": "2025"},
        {"fieldId": "state", "value": "Oregon"},
        {"fieldId": "number_of_dependents", "value": "00"},
        {"fieldId": "income_sources", "value": '["rental","W-2 wages"]'},
        {"fieldId": "deductions", "value": "[]"},
        {"fieldId": "notes", "value": notes},
    ]
    saved = import_values(store, detail, extracted)
    document = saved["documents"][0]
    assert store.get_text(saved["id"], document["id"]) == CONTENT.decode()
    for extracted_value in extracted:
        item = field(saved, extracted_value["fieldId"])
        candidate = item["candidates"][0]
        assert item["valueOrigin"] == "ai"
        assert item["status"] == "needs_review"
        assert not item["conflict"]
        assert item["citations"] == candidate["citations"] == []
        assert item["selectedCandidateId"] == candidate["id"]
        assert candidate["provenance"] == "gemini"
        assert candidate["documentId"] == document["id"]
    assert field(saved, "state")["value"] == "OR"
    assert field(saved, "number_of_dependents")["value"] == "0"
    assert field(saved, "notes")["value"] == notes.strip()
    assert field(saved, "phone")["value"] is None
    assert import_values(store, saved, extracted) == saved
    assert ReviewStore(store.data_dir).get_return(saved["id"]) == saved


def test_conflicting_drafts_preserve_original_and_need_explicit_resolution(store, detail):
    first = {"fieldId": "phone", "value": "503-555-0142"}
    second = {"fieldId": "phone", "value": "503-555-0999"}
    saved = import_values(store, detail, [first])
    original = copy.deepcopy(field(saved, "phone"))
    saved = import_values(store, saved, [second])
    item = field(saved, "phone")
    assert item["value"] == original["value"]
    assert item["selectedCandidateId"] == original["selectedCandidateId"]
    assert item["valueOrigin"] == "ai"
    assert item["conflict"] and item["status"] == "blocked"
    assert [candidate["value"] for candidate in item["candidates"]] == [first["value"], second["value"]]
    assert import_values(store, saved, [first]) == saved
    saved = store.resolve(
        saved["id"], saved["revision"], "phone", item["candidates"][-1]["id"], "Confirmed with client"
    )
    chosen = field(saved, "phone")
    assert chosen["value"] == second["value"]
    assert chosen["valueOrigin"] == "ai"
    assert chosen["status"] == "needs_review" and not chosen["conflict"]
    assert chosen["citations"] == []
    assert len(chosen["candidates"]) == 2
    assert "AI draft from contact.txt" in saved["events"][-1]["message"]


@pytest.mark.parametrize("human_value", ["503-555-0142", "503-555-0999", ""])
def test_drafts_never_replace_or_retag_preparer_edits(store, detail, human_value):
    saved = store.correct(
        detail["id"], detail["revision"], "phone", human_value, [], "Client called"
    )
    original = copy.deepcopy(field(saved, "phone"))
    saved = import_values(store, saved, [{"fieldId": "phone", "value": "503-555-0142"}])
    item = field(saved, "phone")
    for key in ("value", "valueOrigin", "correctionReason", "citations", "selectedCandidateId"):
        assert item[key] == original[key]
    assert item["conflict"] == (human_value != "503-555-0142")
    assert item["status"] == ("blocked" if item["conflict"] else "needs_review")
    assert len(item["candidates"]) == 1


@pytest.mark.parametrize("literal_first", [True, False])
def test_corroboration_keeps_selected_ai_or_literal_source_provenance(store, detail, literal_first):
    draft = {"fieldId": "phone", "value": "503-555-0142"}
    proposals = [literal_phone(), draft] if literal_first else [draft, literal_phone()]
    saved = import_values(store, detail, [proposals[0]])
    selected = copy.deepcopy(field(saved, "phone"))
    saved = import_values(store, saved, [proposals[1]])
    item = field(saved, "phone")
    for key in ("value", "valueOrigin", "citations", "selectedCandidateId", "status", "conflict"):
        assert item[key] == selected[key]
    assert len(item["candidates"]) == 2
    source_candidate = next(
        candidate for candidate in item["candidates"]
        if any(not citation.get("localMatch") for citation in candidate["citations"])
    )
    draft_candidate = next(
        candidate for candidate in item["candidates"]
        if all(citation.get("localMatch") for citation in candidate["citations"])
    )
    assert source_candidate["citations"][0]["quote"] == "Phone: 503-555-0142"
    for candidate, origin in ((source_candidate, "source"), (draft_candidate, "ai")):
        saved = store.resolve(saved["id"], saved["revision"], "phone", candidate["id"])
        assert field(saved, "phone")["valueOrigin"] == origin
        assert field(saved, "phone")["status"] == "needs_review"


def test_invalid_normalized_draft_rolls_back_attached_original(store, detail):
    with pytest.raises(ReviewError):
        import_values(store, detail, [{"fieldId": "state", "value": "Unknown state"}])
    assert store.get_return(detail["id"]) == detail
    with store._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_documents").fetchone()[0] == 0
