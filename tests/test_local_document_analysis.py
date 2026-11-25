from pathlib import Path

import pytest

from src.preparation.local_analysis import analyze_document
from src.review_documents import extract_region


def test_intake_notes_do_not_turn_mentioned_form_amounts_into_source_facts():
    result = analyze_document(
        b"Subject: Please review my tax documents\nForm W-2\nBox 1 wages: $42,150.25\n",
        "text/plain",
    )
    assert result["category"] == "Intake correspondence"
    assert result["forms"] == ["W-2"]
    assert result["facts"] == []


def test_conflicting_duplicate_box_is_excluded_but_other_box_keeps_literal_evidence():
    result = analyze_document(
        b"Form W-2\nEmployer: Example Payroll\nBox 1 wages: $42,150.25\n"
        b"Box 1 wages: $52,150.25\nBox 2 Federal income tax withheld: $0.00\n",
        "text/plain",
    )
    assert result["status"] == "needs_review"
    fact, = result["facts"]
    assert (fact["value"], fact["page"], fact["line"], fact["quote"]) == (
        "0.00", None, 5, "Box 2 Federal income tax withheld: $0.00",
    )


@pytest.mark.parametrize(("filename", "expected"), [
    ("11_sample_w2_pitt_payroll.pdf", {"1": "44629.35", "2": "7631.62", "3": "48736.35"}),
    ("12_sample_1099_nec_taxbit.pdf", {"1a": "12500.00", "4": "0.00"}),
    ("13_sample_1099_k_taxbit.pdf", {"1a": "37042.43", "1b": "22966.31", "4": "0.00"}),
])
def test_published_tax_boxes_have_literal_independent_pdf_regions(filename, expected):
    content = (Path(__file__).resolve().parents[1] / "sample_inputs" / filename).read_bytes()
    result = analyze_document(content, "application/pdf")
    by_box = {fact["label"].split()[1]: fact for fact in result["facts"]}
    for box, value in expected.items():
        fact = by_box[box]
        assert fact["value"] == value
        assert extract_region(content, fact["page"], fact["rect"]) == fact["quote"]
    assert len({by_box[box]["rect"] for box in expected}) == len(expected)
