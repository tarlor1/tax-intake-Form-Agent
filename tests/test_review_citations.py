"""Local citations enrich normalized drafts without replacing AI or user provenance."""

from __future__ import annotations

import copy

import pytest

from src.review_documents import source_pages
from src.review_intake import validate_extraction
from src.review_jobs import _signature
from src.review_store import ReviewStore, _candidate_origin
from review_fixtures import source


NAME = "Avery Morgan"
DOB = "1988-05-12"


def field(detail, identifier):
    return next(item for item in detail["fields"] if item["id"] == identifier)


def normalized_values(**overrides):
    record = {
        "full_name": NAME,
        "email": None,
        "phone": None,
        "date_of_birth": DOB,
        "street_address": None,
        "city": None,
        "state": None,
        "zip_code": None,
        "drivers_license_number": None,
        "drivers_license_state": None,
        "drivers_license_expires_on": None,
        "tax_year": 2025,
        "urgency": {"level": "medium", "reason": "Routine preparation."},
        "missing_fields": [],
    }
    record.update(overrides)
    return validate_extraction({"groups": [{
        "clientName": record["full_name"], "returnType": "individual", "record": record,
    }]})[0]["values"]


def import_values(store, detail, content, values, media_type="text/plain"):
    with store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = store._load(connection, detail["id"])
        store.import_extracted_group(
            connection, current, filename="intake.txt" if media_type == "text/plain" else "intake.pdf",
            content=content, media_type=media_type, values=values,
            pages=source_pages(content, media_type),
        )
        connection.commit()
    return store.get_return(detail["id"])


def new_return(store, name=NAME, year=2025):
    return store.create_return(year, client_name=name, return_type="individual")


@pytest.mark.parametrize("media_type", ["text/plain", "application/pdf"])
@pytest.mark.parametrize("date_text", ["May 12, 1988", "12 May 1988", "May 12th, 1988"])
def test_normalized_dob_locates_literal_prose_and_real_viewer_coordinates(tmp_path, media_type, date_text):
    rows = [f"Client: {NAME}", "Tax year: 2025", f"The client was born on {date_text}."]
    content = "\n".join(rows).encode()
    if media_type == "application/pdf":
        _, content, _ = source("dob", "Client intake", rows)
    store = ReviewStore(tmp_path)
    saved = import_values(store, new_return(store), content, normalized_values(), media_type)
    dob = field(saved, "date_of_birth")
    citation = dob["citations"][0]
    assert dob["value"] == DOB
    assert dob["valueOrigin"] == "ai"
    assert dob["status"] == "needs_review"
    assert citation["quote"] == rows[-1]
    assert citation["localMatch"] is True
    assert DOB not in citation["quote"]
    pages = source_pages(content, media_type)
    expected_index = next(index for index, line in enumerate(pages[0]["lines"]) if line["text"] == rows[-1])
    assert citation["rect"] == pytest.approx(pages[0]["lines"][expected_index]["rect"])
    if media_type == "text/plain":
        assert citation["lineStart"] == citation["lineEnd"] == expected_index + 1
    candidate = dob["candidates"][0]
    assert candidate["provenance"] == "gemini"
    assert _candidate_origin(candidate) == "ai"
    assert candidate["citations"] == dob["citations"]


def test_exact_case_whitespace_and_state_labels_keep_literal_quotes(tmp_path):
    store = ReviewStore(tmp_path)
    content = b"Client: Avery Morgan\nTax year: 2025\nOccupation: SENIOR   ENGINEER\nState: Oregon\n"
    saved = import_values(
        store, new_return(store), content,
        normalized_values(occupation="Senior Engineer", state="OR"),
    )
    assert field(saved, "occupation")["citations"][0]["quote"] == "Occupation: SENIOR   ENGINEER"
    assert field(saved, "state")["citations"][0]["quote"] == "State: Oregon"


def test_inference_and_missing_source_never_block_or_invent_a_quote(tmp_path):
    store = ReviewStore(tmp_path)
    saved = import_values(
        store, new_return(store), b"Client: Avery Morgan\nTax year: 2025\nWorks on bridges.\n",
        normalized_values(occupation="Civil engineer", request_summary="Prepare a routine return."),
    )
    for identifier in ("date_of_birth", "occupation", "request_summary", "urgency"):
        item = field(saved, identifier)
        assert item["value"] is not None
        assert item["citations"] == []
        assert item["candidates"][0]["documentId"] == saved["documents"][0]["id"]


def test_no_cross_client_date_when_only_other_client_has_matching_date(tmp_path):
    store = ReviewStore(tmp_path)
    content = (
        b"Client: Avery Morgan\nTax year: 2025\nBirth date not given.\n"
        b"Client: Jordan Example\nTax year: 2025\nBorn on May 12, 1988.\n"
    )
    saved = import_values(store, new_return(store), content, normalized_values())
    assert field(saved, "date_of_birth")["citations"] == []


def test_identical_dates_are_cited_only_in_matching_client_section(tmp_path):
    store = ReviewStore(tmp_path)
    content = (
        b"Client: Jordan Example\nTax year: 2025\nDOB: May 12, 1988\n"
        b"Client: Avery Morgan\nTax year: 2025\nDOB: May 12, 1988\n"
    )
    saved = import_values(store, new_return(store), content, normalized_values())
    assert field(saved, "date_of_birth")["citations"][0]["lineStart"] == 6


def test_repeated_unscoped_date_and_shared_client_sentence_remain_uncited(tmp_path):
    store = ReviewStore(tmp_path)
    shared = (
        b"Avery Morgan and Jordan Example requested returns for 2025.\n"
        b"Born on May 12, 1988.\n"
    )
    first = import_values(store, new_return(store), shared, normalized_values())
    import_values(
        store, new_return(store, "Jordan Example"), shared,
        normalized_values(full_name="Jordan Example"),
    )
    assert field(store.get_return(first["id"]), "date_of_birth")["citations"] == []
    repeated = import_values(
        store, first, b"DOB: May 12, 1988\nDOB: May 12, 1988\n", normalized_values(),
    )
    assert field(repeated, "date_of_birth")["candidates"][-1]["citations"] == []


def test_other_tax_year_is_not_used_as_evidence(tmp_path):
    store = ReviewStore(tmp_path)
    content = b"Client: Avery Morgan\nTax year: 2024\nDOB: May 12, 1988\nTax year: 2025\n"
    saved = import_values(store, new_return(store), content, normalized_values())
    assert field(saved, "date_of_birth")["citations"] == []


def test_shared_client_birth_date_remains_citable_across_requested_years(tmp_path):
    store = ReviewStore(tmp_path)
    content = b"Client: Avery Morgan\nDOB: May 12, 1988\n"
    current = import_values(store, new_return(store), content, normalized_values())
    import_values(
        store, new_return(store, year=2024), content, normalized_values(tax_year=2024),
    )
    for detail in store.list_returns():
        dob = field(store.get_return(detail["id"]), "date_of_birth")
        assert dob["citations"][0]["quote"] == "DOB: May 12, 1988"


def test_legacy_candidates_enrich_on_read_without_ai_or_storage_mutation(tmp_path, monkeypatch):
    store = ReviewStore(tmp_path)
    saved = import_values(
        store, new_return(store), b"Client: Avery Morgan\nTax year: 2025\nDOB: May 12, 1988\n",
        normalized_values(),
    )
    with store._connection() as connection:
        raw = store._load(connection, saved["id"])
        original_payload = connection.execute(
            "SELECT payload FROM review_returns WHERE id = ?", (saved["id"],)
        ).fetchone()["payload"]
    assert field(raw, "date_of_birth")["citations"] == []
    original = copy.deepcopy(raw)

    def forbidden(*args, **kwargs):
        raise AssertionError("Legacy local citation enrichment cannot call Gemini")

    monkeypatch.setattr("src.review_intake.ReviewIntake._call_gemini", forbidden)
    reopened = ReviewStore(tmp_path)
    enriched = reopened.get_return(saved["id"])
    assert field(enriched, "date_of_birth")["citations"]
    assert _signature(enriched) == _signature(original)
    assert enriched["revision"] == original["revision"]
    assert enriched["events"] == original["events"]
    assert field(enriched, "date_of_birth")["revision"] == field(original, "date_of_birth")["revision"]
    assert enriched == reopened.get_return(saved["id"])
    with reopened._connection() as connection:
        assert connection.execute(
            "SELECT payload FROM review_returns WHERE id = ?", (saved["id"],)
        ).fetchone()["payload"] == original_payload


def test_user_correction_and_candidate_selection_keep_existing_decisions(tmp_path):
    store = ReviewStore(tmp_path)
    saved = import_values(
        store, new_return(store), b"Client: Avery Morgan\nTax year: 2025\nDOB: May 12, 1988\n",
        normalized_values(),
    )
    candidate = field(saved, "date_of_birth")["candidates"][0]
    corrected = store.correct(saved["id"], saved["revision"], "date_of_birth", "1988-05-13", [], "Client correction")
    item = field(corrected, "date_of_birth")
    assert item["valueOrigin"] == "preparer"
    assert item["correctionReason"] == "Client correction"
    assert item["citations"] == []
    assert item["candidates"][0]["citations"]
    chosen = store.resolve(saved["id"], corrected["revision"], "date_of_birth", candidate["id"])
    assert field(chosen, "date_of_birth")["valueOrigin"] == "ai"
    assert field(chosen, "date_of_birth")["citations"]


@pytest.mark.parametrize("evidence", [{}, {"page": 999, "quote": "invented"}, "invalid", {"page": [], "lineStart": 1}])
def test_malformed_optional_evidence_never_rejects_normalized_drafts(tmp_path, evidence):
    store = ReviewStore(tmp_path)
    values = [{"fieldId": "date_of_birth", "value": DOB, "evidence": evidence}]
    saved = import_values(store, new_return(store), b"DOB: May 12, 1988\n", values)
    assert field(saved, "date_of_birth")["value"] == DOB
    assert field(saved, "date_of_birth")["citations"][0]["quote"] == "DOB: May 12, 1988"
