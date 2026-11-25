from hashlib import sha256

import pytest
from pydantic import ValidationError

from src.sigma_demo.pdf import source
from src.preparation.contracts import (
    Amount,
    Candidate,
    DocumentEvidence,
    ExactClaim,
    LineEvidence,
    MissingValue,
    OriginalRef,
    PageEvidence,
    RecordIdentity,
    RegionEvidence,
    SourceOccurrence,
    TextValue,
)
from src.preparation.evidence import OriginalScope, StoredOriginal, VerifiedFact, ground_occurrence, load_original
from src.review_documents import DocumentError, extract_region


ORIGINAL = OriginalRef(document_id="w2-upload", version_id="upload-v1")
SCOPE = OriginalScope(firm_id="firm-a", return_id="return-a-2025", original=ORIGINAL)
W2 = RecordIdentity(tax_year=2025, form="w2", subject_reference="employee-a", issuer_reference="employer-a",
                    activity_reference=None, period=None, instance_reference="instance-a")


def stored(content, media_type="text/plain", scope=SCOPE):
    return StoredOriginal(scope, media_type, sha256(content).hexdigest(), content)


def loaded(content, media_type="text/plain"):
    original = stored(content, media_type)
    return load_original(SCOPE, lambda scope: original)


def pdf_source(rows):
    _, content, _ = source("synthetic-w2", "Synthetic Form W-2", rows)
    return loaded(content, "application/pdf")


def exact(line, quote, *, page=1, line_end=None, original=ORIGINAL):
    return ExactClaim(precision="exact", original=original, page=page, line_start=line,
                      line_end=line if line_end is None else line_end, quote=quote)


def candidate(*claims, fact_key="box-2", column="reported", raw_value="$2,000.00", normalized=None):
    return Candidate(fact_key=fact_key, column=column, raw_value=raw_value,
                     normalized=normalized if normalized is not None else Amount(kind="amount", value="2000.00"),
                     citations=claims)


def occurrence(*candidates, identity=W2, original=ORIGINAL):
    return SourceOccurrence(identity=identity, original=original, candidates=candidates)


def verified(item, *anchors, scope=SCOPE, identity=W2):
    return VerifiedFact(scope=scope, identity=identity, fact_key=item.fact_key, column=item.column,
                        raw_value=item.raw_value, normalized=item.normalized, anchors=anchors)


@pytest.mark.parametrize("mismatch", [
    {"firm_id": "firm-b"},
    {"return_id": "return-b"},
    {"original": OriginalRef(document_id="another-upload", version_id="upload-v1")},
    {"original": OriginalRef(document_id="w2-upload", version_id="upload-v2")},
])
def test_original_loader_requires_exact_scope(mismatch):
    calls = []

    def read_original(scope):
        calls.append(scope)
        return stored(b"Different protected original", scope=SCOPE.model_copy(update=mismatch))

    with pytest.raises(DocumentError) as err:
        load_original(SCOPE, read_original)
    assert err.value.status_code == 404
    assert calls == [SCOPE]


def test_original_bytes_must_match_immutable_version_metadata():
    original = stored(b"Original bytes")
    replaced = StoredOriginal(SCOPE, original.media_type, original.digest, b"Different bytes")
    with pytest.raises(DocumentError, match="does not match its stored version"):
        load_original(SCOPE, lambda scope: replaced)


def test_pdf_grounding_uses_trusted_box_not_the_first_equal_amount():
    wages = "Box 1 wages: $2,000.00"
    withholding = "Box 2 withholding: $2,000.00"
    original = pdf_source([wages, withholding])
    numbered, = original.numbered_pages()
    assert numbered["page"] == 1
    assert numbered["lines"][2:4] == [{"line": 3, "text": wages}, {"line": 4, "text": withholding}]

    claim = exact(4, "$2,000.00")
    item = candidate(claim)
    grounded, = ground_occurrence(original, occurrence(item), [verified(item, claim)])
    anchor, = grounded.citations
    assert grounded.scope == SCOPE and grounded.eligible
    assert isinstance(anchor, RegionEvidence)
    assert anchor.original == ORIGINAL and anchor.page == 1 and anchor.quote == "$2,000.00"
    assert extract_region(original.original.content, anchor.page, anchor.rect) == withholding

    wrong = exact(3, "$2,000.00")
    wrong_occurrence = occurrence(candidate(wrong))
    unverified, = ground_occurrence(original, wrong_occurrence, [verified(item, claim)])
    assert unverified.citations[0].precision == "document"
    known_page = PageEvidence(precision="page", original=ORIGINAL, page=1)
    grounded, = ground_occurrence(original, wrong_occurrence, [verified(item, claim, known_page)])
    assert grounded.citations == (known_page,) and grounded.eligible


@pytest.mark.parametrize("identity_change", [
    {"subject_reference": "employee-b"},
    {"issuer_reference": "employer-b"},
    {"tax_year": 2024},
    {"instance_reference": "instance-b"},
    {"activity_reference": "other-activity"},
])
def test_equal_literal_does_not_verify_a_different_record(identity_change):
    line = "Box 2 withholding: $2,000.00"
    original = pdf_source([line])
    claim = exact(3, line)
    item = candidate(claim)
    other_record = W2.model_copy(update=identity_change)
    grounded, = ground_occurrence(original, occurrence(item), [verified(item, claim, identity=other_record)])
    assert grounded.citations == (DocumentEvidence(precision="document", original=ORIGINAL),)
    assert not grounded.eligible


def test_facts_from_another_return_or_version_cannot_verify_the_page():
    line = "Box 2 withholding: $2,000.00"
    original = pdf_source([line])
    claim = exact(3, line)
    item = candidate(claim)
    other_original = OriginalRef(document_id="w2-upload", version_id="upload-v2")
    other_version = SCOPE.model_copy(update={"original": other_original})
    grounded, = ground_occurrence(original, occurrence(item), [
        verified(item, claim, scope=SCOPE.model_copy(update={"return_id": "other-return"})),
        verified(item, claim, scope=SCOPE.model_copy(update={"firm_id": "other-firm"})),
        verified(item, exact(3, line, original=other_original), scope=other_version),
    ])
    assert grounded.citations == (DocumentEvidence(precision="document", original=ORIGINAL),)
    with pytest.raises(DocumentError) as err:
        ground_occurrence(original, occurrence(original=other_original), [])
    assert err.value.status_code == 404


@pytest.mark.parametrize("page", [None, 2, 99])
def test_unknown_pdf_page_keeps_only_original_even_when_another_page_is_verified(page):
    line = "Box 2 withholding: $2,000.00"
    original = pdf_source([line])
    claim = exact(3, line, page=page)
    item = candidate(claim)
    grounded, = ground_occurrence(original, occurrence(item), [verified(item, exact(3, line))])
    anchor, = grounded.citations
    assert anchor.model_dump() == {"precision": "document", "original": ORIGINAL.model_dump()}
    assert not grounded.eligible


def test_in_range_page_requires_independent_verification():
    original = pdf_source(["Box 2 withholding: $2,000.00"])
    claimed_page = PageEvidence(precision="page", original=ORIGINAL, page=1)
    item = candidate(claimed_page)
    unverified, = ground_occurrence(original, occurrence(item), [])
    verified_page, = ground_occurrence(original, occurrence(item), [verified(item, claimed_page)])
    assert unverified.citations[0].precision == "document" and not unverified.eligible
    assert verified_page.citations == (claimed_page,) and verified_page.eligible


def test_nonliteral_quote_cannot_establish_a_page():
    original = pdf_source(["Box 2: $2,000.00"])
    claim = exact(3, "Box 2: $9,999.00")
    item = candidate(claim)
    grounded, = ground_occurrence(original, occurrence(item), [verified(item, claim)])
    assert grounded.citations == (DocumentEvidence(precision="document", original=ORIGINAL),)


def test_ambiguous_pdf_region_has_no_highlight():
    original = pdf_source(["Previously reported $2,000.00; correct information $2,000.00"])
    claim = exact(3, "$2,000.00")
    item = candidate(claim)
    grounded, = ground_occurrence(original, occurrence(item), [verified(item, claim)])
    anchor, = grounded.citations
    assert anchor.model_dump() == {"precision": "page", "original": ORIGINAL.model_dump(), "page": 1}


def test_txt_citation_preserves_literal_text_without_synthesizing_a_page():
    original = loaded("\ufeffReceipt subject: employee-a\r\n\r\nPayment recorded:\r\nMay 12, 2025\r\n".encode())
    assert original.numbered_pages() == [{"page": None, "lines": [
        {"line": 1, "text": "Receipt subject: employee-a"},
        {"line": 2, "text": ""},
        {"line": 3, "text": "Payment recorded:"},
        {"line": 4, "text": "May 12, 2025"},
    ]}]
    literal = "Payment recorded:\nMay 12, 2025"
    claim = exact(3, literal, page=None, line_end=4)
    false_page = exact(3, literal, page=1, line_end=4)
    item = candidate(claim, false_page, fact_key="payment-date", raw_value="May 12, 2025",
                     normalized=TextValue(kind="text", value="2025-05-12"))
    receipt = W2.model_copy(update={"form": "receipt"})
    grounded, = ground_occurrence(original, occurrence(item, identity=receipt),
                                  [verified(item, claim, false_page, identity=receipt)])
    assert grounded.citations == (
        LineEvidence(precision="lines", original=ORIGINAL, line_start=3, line_end=4, quote=literal),
        DocumentEvidence(precision="document", original=ORIGINAL),
    )
    assert grounded.eligible and grounded.candidate.normalized == item.normalized


def test_w2c_wrong_column_or_value_never_verifies_explicit_zero():
    line = "Box 2 Correct information: $0.00"
    original = pdf_source(["Box 2 Previously reported: $42.00", line])
    claim = exact(4, line)
    correction = W2.model_copy(update={"form": "w2c"})
    item = candidate(claim, column="correct_information", raw_value="$0.00", normalized=Amount(kind="amount", value="0"))
    prior_column = candidate(claim, column="previously_reported", raw_value="$0.00", normalized=item.normalized)
    wrong_value = candidate(claim, column="correct_information", raw_value="$0.00")
    unverified, = ground_occurrence(original, occurrence(item, identity=correction), [
        verified(prior_column, claim, identity=correction),
        verified(wrong_value, claim, identity=correction),
    ])
    grounded, = ground_occurrence(original, occurrence(item, identity=correction),
                                  [verified(item, claim, identity=correction)])
    assert not unverified.eligible and unverified.citations[0].precision == "document"
    assert grounded.eligible and grounded.citations[0].precision == "region"


def test_documented_missing_value_is_not_eligible_for_entry():
    line = "Box 2 withheld amount unavailable."
    original = loaded(line.encode())
    claim = exact(1, line, page=None)
    item = candidate(claim, raw_value="unavailable", normalized=MissingValue(kind="missing"))
    grounded, = ground_occurrence(original, occurrence(item), [verified(item, claim)])
    assert grounded.citations[0].precision == "lines" and not grounded.eligible


def test_provider_cannot_include_coordinates_in_an_exact_claim():
    claim = exact(3, "Box 2 withholding: $2,000.00")
    with pytest.raises(ValidationError, match="rect"):
        ExactClaim.model_validate({**claim.model_dump(), "rect": [0.1, 0.2, 0.3, 0.4]})
