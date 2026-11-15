"""Immutable originals and scoped human proposals for actual intake controls."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from review_fixtures import INDIVIDUAL_ID, PRIOR_YEAR_ID, corrected_source, seeded_store, source
from src.review_store import ReviewError, ReviewStore


@pytest.fixture
def store(tmp_path):
    return seeded_store(tmp_path / "workspace")


@pytest.fixture
def evidence():
    _, pdf, citations = source(
        "contact-evidence",
        "Client contact instructions",
        [
            "Client: Avery Morgan | Tax year: 2025",
            "Phone: 503-555-0142",
            "Preferred contact method: Phone",
            "Number of dependents: 0",
        ],
    )
    return pdf, citations


def field(detail, identifier):
    return next(item for item in detail["fields"] if item["id"] == identifier)


def upload(store, detail, pdf, filename="contact.pdf"):
    return store.add_document(detail["id"], detail["revision"], filename, pdf)


def propose(store, detail, document_id, field_id, rect, value, reason=""):
    return store.add_proposal(
        detail["id"], detail["revision"], field_id, document_id, 1, rect, value, reason
    )


def test_real_upload_is_immutable_deduplicated_and_does_not_change_values(store, evidence):
    detail = store.get_return(INDIVIDUAL_ID)
    pdf, _ = evidence
    added = upload(store, detail, pdf)
    assert added["fields"] == detail["fields"]
    document = added["documents"][-1]
    assert store.get_pdf(detail["id"], document["id"]) == pdf
    assert upload(store, added, pdf, "renamed.pdf") == added
    assert ReviewStore(store.data_dir).get_return(detail["id"]) == added


def test_existing_and_hidden_originals_keep_their_document_identity(store, evidence):
    detail = store.get_return(INDIVIDUAL_ID)
    original_id = detail["documents"][0]["id"]
    assert upload(store, detail, store.get_pdf(detail["id"], original_id)) == detail
    pdf, _ = evidence
    with store._connection() as connection:
        connection.execute(
            "INSERT INTO review_documents VALUES (?, ?, ?)", (detail["id"], "hidden-original", pdf)
        )
        connection.commit()
    added = upload(store, detail, pdf)
    assert added["documents"][-1]["id"] == "hidden-original"
    assert upload(store, added, pdf) == added


def test_identical_uploads_are_scoped_to_exact_return_and_year(store, evidence):
    pdf, citations = evidence
    current = upload(store, store.get_return(INDIVIDUAL_ID), pdf)
    prior = upload(store, store.get_return(PRIOR_YEAR_ID), pdf)
    current_id, prior_id = current["documents"][-1]["id"], prior["documents"][-1]["id"]
    assert current_id != prior_id
    with pytest.raises(ReviewError):
        store.get_pdf(prior["id"], current_id)
    updated = propose(store, current, current_id, "phone", citations[1]["rect"], "503-555-0142")
    assert field(updated, "phone")["value"] == "503-555-0142"
    assert store.get_return(prior["id"]) == prior


def test_region_quote_and_native_enum_are_available_to_real_consumers(store, evidence):
    pdf, citations = evidence
    detail = upload(store, store.get_return(INDIVIDUAL_ID), pdf)
    document_id = detail["documents"][-1]["id"]
    detail = propose(
        store, detail, document_id, "preferred_contact_method", citations[2]["rect"], "Phone"
    )
    item = field(detail, "preferred_contact_method")
    assert item["value"] == "phone"
    assert item["valueOrigin"] == "source"
    assert item["citations"][0]["quote"] == "Preferred contact method: Phone"
    assert item["candidates"][0]["provenance"] == "human"
    detail = propose(store, detail, document_id, "number_of_dependents", citations[3]["rect"], "0")
    assert field(detail, "number_of_dependents")["value"] == "0"


def test_conflicting_source_preserves_human_value_and_unrelated_fields(store, evidence):
    detail = store.get_return(INDIVIDUAL_ID)
    detail = store.correct(
        detail["id"], detail["revision"], "phone", "503-555-0999", [], "Called client"
    )
    before = detail
    pdf, citations = evidence
    detail = upload(store, detail, pdf)
    detail = propose(
        store, detail, detail["documents"][-1]["id"], "phone", citations[1]["rect"], "503-555-0142"
    )
    item = field(detail, "phone")
    assert item["value"] == "503-555-0999"
    assert item["valueOrigin"] == "preparer"
    assert item["citations"] == []
    assert item["conflict"]
    for identifier in ("email", "employer", "street_address"):
        assert field(detail, identifier) == field(before, identifier)
    chosen = store.resolve(
        detail["id"],
        detail["revision"],
        "phone",
        item["candidates"][-1]["id"],
        reason="Confirmed contact instructions",
    )
    assert field(chosen, "phone")["value"] == "503-555-0142"
    assert field(chosen, "phone")["valueOrigin"] == "source"
    assert "Confirmed contact instructions" in chosen["events"][-1]["message"]


def test_same_value_corroboration_does_not_retag_preparer_as_source(store, evidence):
    detail = store.get_return(INDIVIDUAL_ID)
    detail = store.correct(detail["id"], detail["revision"], "phone", "503-555-0142", [])
    pdf, citations = evidence
    detail = upload(store, detail, pdf)
    detail = propose(
        store, detail, detail["documents"][-1]["id"], "phone", citations[1]["rect"], "503-555-0142"
    )
    assert field(detail, "phone")["valueOrigin"] == "preparer"
    assert field(detail, "phone")["citations"] == []
    assert not field(detail, "phone")["conflict"]


def test_cleared_preparer_value_is_not_repopulated_by_later_source(store, evidence):
    detail = store.get_return(INDIVIDUAL_ID)
    detail = store.correct(detail["id"], detail["revision"], "phone", "", [])
    pdf, citations = evidence
    detail = upload(store, detail, pdf)
    detail = propose(
        store, detail, detail["documents"][-1]["id"], "phone", citations[1]["rect"], "503-555-0142"
    )
    assert field(detail, "phone")["value"] is None
    assert field(detail, "phone")["conflict"]
    assert field(detail, "phone")["candidates"][-1]["value"] == "503-555-0142"


@pytest.mark.parametrize(
    "page,rect",
    [
        (0, [0, 0, 1, 1]),
        (2, [0, 0, 1, 1]),
        (1, [-0.1, 0, 1, 1]),
        (1, [0, 0, 0, 1]),
        (1, [0, 0, 2, 1]),
        (1, [0, float("nan"), 1, 1]),
    ],
)
def test_invalid_geometry_is_atomic(store, evidence, page, rect):
    pdf, _ = evidence
    detail = upload(store, store.get_return(INDIVIDUAL_ID), pdf)
    with pytest.raises(ReviewError):
        store.add_proposal(
            detail["id"],
            detail["revision"],
            "phone",
            detail["documents"][-1]["id"],
            page,
            rect,
            "503-555-0142",
        )
    assert store.get_return(detail["id"]) == detail


@pytest.mark.parametrize(
    "identifier,value,reason",
    [
        ("urgency", "urgent", ""),
        ("number_of_dependents", "1.5", ""),
        ("date_of_birth", "not a date", ""),
        ("phone", "", ""),
        ("phone", "x" * 501, ""),
        ("phone", "503-555-0142", "x" * 2001),
        ("primary-wages", "72000", ""),
    ],
)
def test_invalid_proposals_are_atomic(store, evidence, identifier, value, reason):
    pdf, citations = evidence
    detail = upload(store, store.get_return(INDIVIDUAL_ID), pdf)
    with pytest.raises(ReviewError):
        propose(
            store,
            detail,
            detail["documents"][-1]["id"],
            identifier,
            citations[1]["rect"],
            value,
            reason,
        )
    assert store.get_return(detail["id"]) == detail


def test_image_only_and_empty_regions_never_invent_quote(store, evidence):
    image = Image.new("RGB", (100, 100), color="white")
    output = io.BytesIO()
    image.save(output, format="PDF")
    detail = upload(store, store.get_return(INDIVIDUAL_ID), output.getvalue())
    with pytest.raises(ReviewError):
        propose(store, detail, detail["documents"][-1]["id"], "phone", [0, 0, 1, 1], "503-555-0142")
    assert store.get_return(detail["id"]) == detail
    pdf, _ = evidence
    detail = upload(store, detail, pdf)
    with pytest.raises(ReviewError):
        propose(
            store,
            detail,
            detail["documents"][-1]["id"],
            "phone",
            [0.8, 0.8, 0.1, 0.1],
            "503-555-0142",
        )
    assert store.get_return(detail["id"]) == detail


@pytest.mark.parametrize("pdf", [b"", b"not a PDF", b"%PDF-1.7\n%%EOF\n"])
def test_malformed_uploads_are_atomic(store, pdf):
    detail = store.get_return(INDIVIDUAL_ID)
    with pytest.raises(ReviewError):
        upload(store, detail, pdf)
    assert store.get_return(detail["id"]) == detail


def test_stale_upload_and_proposal_do_not_partially_write(store, evidence):
    original = store.get_return(INDIVIDUAL_ID)
    pdf, citations = evidence
    detail = upload(store, original, pdf)
    with pytest.raises(ReviewError):
        upload(store, original, pdf)
    with pytest.raises(ReviewError):
        propose(
            store,
            original,
            detail["documents"][-1]["id"],
            "phone",
            citations[1]["rect"],
            "503-555-0142",
        )
    assert store.get_return(detail["id"]) == detail


def test_original_and_corrected_w2_are_retained_without_mapping_amounts(store):
    detail = store.get_return(INDIVIDUAL_ID)
    _, pdf, _ = corrected_source()
    updated = upload(store, detail, pdf, "corrected-w2.pdf")
    assert updated["fields"] == detail["fields"]
    assert updated["legacyFields"] == detail["legacyFields"]
    assert store.get_pdf(updated["id"], updated["documents"][-1]["id"]) == pdf
    assert any(document["id"] == "morgan-w2-original" for document in updated["documents"])


def test_txt_manual_proposals_keep_real_lines_and_original_utf8(tmp_path):
    store = ReviewStore(tmp_path)
    detail = store.create_return(2025, client_name="Client", return_type="individual")
    original = "Client: Client\nPhone: 503-555-0101\nNotes: café\n"
    detail = store.add_document(
        detail["id"], detail["revision"], "source.txt", original.encode(), "text/plain"
    )
    document_id = detail["documents"][0]["id"]
    detail = propose(store, detail, document_id, "phone", [0, 1 / 3, 1, 1 / 3], "503-555-0101")
    citation = field(detail, "phone")["citations"][0]
    assert citation["quote"] == "Phone: 503-555-0101"
    assert citation["lineStart"] == citation["lineEnd"] == 2
    assert store.get_text(detail["id"], document_id) == original
