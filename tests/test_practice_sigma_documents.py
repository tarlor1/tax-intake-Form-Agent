from hashlib import sha256

import pytest

from src.sigma_demo.documents import SUBJECT_REFERENCE, TAX_YEAR, extract_numbered, verify_document
from src.sigma_demo.sources import individual_sources
from src.preparation.contracts import OriginalRef
from src.preparation.evidence import OriginalScope, StoredOriginal, ground_occurrence, load_original
from src.preparation.upload_processing import _numbered_text


@pytest.mark.parametrize("index,form,fact_count", [(0, "w2", 2), (1, "w2", 2), (2, "1099k", 1),
                                                    (3, "platform_summary", 3), (4, "1099nec", 1), (5, "receipt", 4)])
def test_practice_originals_ground_scoped_exact_facts(index, form, fact_count):
    source = individual_sources()[index]
    # Opaque IDs contain no form, recipient, or fixture filename.
    ref = OriginalRef(document_id="document", version_id="version-9")
    scope = OriginalScope(firm_id="firm", return_id="return", original=ref)
    original = StoredOriginal(scope, source.media_type, sha256(source.content).hexdigest(), source.content)
    loaded = load_original(scope, lambda _: original)
    extraction = extract_numbered(_numbered_text(ref, source.media_type, source.content))
    verified = verify_document(loaded)
    assert not extraction.issues
    assert len(verified) == fact_count
    assert {(fact.scope, fact.identity.form, fact.identity.subject_reference, fact.identity.tax_year) for fact in verified} == {
        (scope, form, SUBJECT_REFERENCE, TAX_YEAR),
    }
    grounded = [fact for occurrence in extraction.records for fact in ground_occurrence(loaded, occurrence, verified)]
    assert len(grounded) == fact_count
    for fact in grounded:
        assert fact.eligible
        assert all(citation.original == ref for citation in fact.citations)
        assert {citation.precision for citation in fact.citations} <= (
            {"region", "page"} if source.media_type == "application/pdf" else {"lines"}
        )
    if form == "receipt":
        assert {fact.identity.instance_reference for fact in grounded} == {"ROTOR-102", "ROTOR-841"}


def test_practice_decoder_rejects_subject_changed_in_literal_bytes():
    summary = individual_sources()[3]
    content = summary.content.replace(SUBJECT_REFERENCE.encode(), b"OTHER-SUBJECT")
    ref = OriginalRef(document_id="claimed-summary", version_id="new-version")
    scope = OriginalScope(firm_id="firm", return_id="return", original=ref)
    original = StoredOriginal(scope, summary.media_type, sha256(content).hexdigest(), content)
    extraction = extract_numbered(_numbered_text(ref, summary.media_type, content))
    assert not verify_document(load_original(scope, lambda _: original))
    assert not extraction.records
    assert [(issue.original, issue.code) for issue in extraction.issues] == [(ref, "unknown_form")]


def test_wrong_receipt_anchor_with_equal_amount_stays_ineligible():
    receipts = individual_sources()[5]
    ref = OriginalRef(document_id="same-document", version_id="same-version")
    scope = OriginalScope(firm_id="firm", return_id="return", original=ref)
    original = StoredOriginal(scope, receipts.media_type, sha256(receipts.content).hexdigest(), receipts.content)
    loaded = load_original(scope, lambda _: original)
    first, second = extract_numbered(_numbered_text(ref, receipts.media_type, receipts.content)).records
    first_amount, = (fact for fact in first.candidates if fact.fact_key == "amount")
    second_amount, = (fact for fact in second.candidates if fact.fact_key == "amount")
    assert first_amount.normalized == second_amount.normalized
    wrong_line = first_amount.model_copy(update={"citations": second_amount.citations})
    wrong_receipt = first.model_copy(update={"candidates": (wrong_line,)})
    fact, = ground_occurrence(loaded, wrong_receipt, verify_document(loaded))
    assert not fact.eligible
    assert fact.citations[0].precision == "document"


def test_matching_box_1_amount_cannot_ground_box_3():
    source = individual_sources(include_social_security_wages=True)[0]
    ref = OriginalRef(document_id="harbor-w2", version_id="sample")
    scope = OriginalScope(firm_id="firm", return_id="return", original=ref)
    original = StoredOriginal(scope, source.media_type, sha256(source.content).hexdigest(), source.content)
    loaded = load_original(scope, lambda _: original)
    occurrence, = extract_numbered(_numbered_text(ref, source.media_type, source.content)).records
    facts = {fact.fact_key: fact for fact in occurrence.candidates}
    box_1, box_3 = facts["box-1"], facts["social_security_wages"]
    assert box_1.normalized == box_3.normalized

    wrong_box = box_3.model_copy(update={"citations": box_1.citations})
    claimed = occurrence.model_copy(update={"candidates": (wrong_box,)})
    grounded, = ground_occurrence(loaded, claimed, verify_document(loaded))
    assert not grounded.eligible
    assert grounded.citations[0].precision == "document"
