"""Explicit synthetic review fixtures for tests only; never loaded by production."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from src.review_store import ReviewStore
from src.review_returns import migrate_return
from src.sigma_demo.pdf import source

INDIVIDUAL_ID = "individual-morgan"
PRIOR_YEAR_ID = "individual-morgan-2024"
BUSINESS_ID = "business-studio"


def candidate(field_id: str, suffix: str, value: str, citation: dict) -> dict:
    return {
        "id": f"{citation['documentId']}-{field_id}-{suffix}",
        "value": value,
        "citations": [citation],
    }


def field(
    field_id: str,
    label: str,
    section: str,
    kind: str,
    candidates: list[dict],
    *,
    conflict: bool = False,
    dependencies: list[str] | None = None,
    value: str | None = None,
    citations: list[dict] | None = None,
    derivation: str | None = None,
) -> dict:
    first = candidates[0] if candidates else None
    proposed = first["value"] if first else value
    return {
        "id": field_id,
        "label": label,
        "section": section,
        "kind": kind,
        "value": proposed,
        "revision": 1,
        "status": "blocked" if conflict or proposed is None else "needs_review",
        "conflict": conflict,
        "selectedCandidateId": first["id"] if first and not conflict else None,
        "candidates": candidates,
        "citations": first["citations"] if first else (citations or []),
        "dependencies": dependencies or [],
        "derivation": derivation,
        "correctionReason": None,
    }


def _individual_scenario() -> dict[str, Any]:
    """A complete 2025 proposed draft, ready before human review begins."""
    w2, w2_pdf, w2_cites = source(
        "morgan-w2-original",
        "Morgan - original W-2 example",
        [
            "Employee: Avery Morgan | Tax year: 2025",
            "Employer: Example Harbor Labs",
            "Box 1 wages: 72000.00",
            "Box 2 federal income tax withheld: 9100.00",
        ],
    )
    other, other_pdf, other_cites = source(
        "morgan-w2-second",
        "Morgan - second W-2 example",
        [
            "Employee: Avery Morgan | Tax year: 2025",
            "Employer: Example Evening School",
            "Box 1 wages: 8500.00",
        ],
    )
    intake, intake_pdf, intake_cites = source(
        "morgan-intake",
        "Morgan - synthetic intake",
        [
            "Client: Avery Morgan | Tax year: 2025",
            "Mailing address: 14 Example Lane, Portland OR 97201",
            "Phone: not supplied",
        ],
    )
    note, note_pdf, note_cites = source(
        "morgan-address-note",
        "Morgan - client address note",
        [
            "Client: Avery Morgan | Tax year: 2025",
            "Mailing address: 28 Sample Avenue, Portland OR 97205",
            "Please use this address instead of the intake address.",
        ],
    )
    primary = field(
        "primary-wages",
        "Primary W-2 wages",
        "Income",
        "money",
        [candidate("primary-wages", "original", "72000.00", w2_cites[2])],
    )
    secondary = field(
        "second-wages",
        "Second W-2 wages",
        "Income",
        "money",
        [candidate("second-wages", "original", "8500.00", other_cites[2])],
    )
    individual = {
        "id": INDIVIDUAL_ID,
        "clientId": "avery-morgan",
        "clientName": "Avery Morgan",
        "taxYear": 2025,
        "returnType": "individual",
        "destination": "Individual worksheet",
        "revision": 1,
        "activeFieldId": "primary-wages",
        "fields": [
            primary,
            field(
                "withholding",
                "Federal withholding",
                "Income",
                "money",
                [candidate("withholding", "original", "9100.00", w2_cites[3])],
            ),
            secondary,
            field(
                "total-wages",
                "Total wages",
                "Calculated",
                "money",
                [],
                value="80500.00",
                citations=primary["citations"] + secondary["citations"],
                dependencies=["primary-wages", "second-wages"],
                derivation="Primary W-2 wages + second W-2 wages (demonstration sum, not tax advice).",
            ),
            field(
                "mailing-address",
                "Mailing address",
                "Client details",
                "text",
                [
                    candidate(
                        "mailing-address",
                        "intake",
                        "14 Example Lane, Portland OR 97201",
                        intake_cites[1],
                    ),
                    candidate(
                        "mailing-address",
                        "client-note",
                        "28 Sample Avenue, Portland OR 97205",
                        note_cites[1],
                    ),
                ],
                conflict=True,
            ),
            field("phone", "Phone number", "Client details", "text", []),
        ],
        "documents": [w2, other, intake, note],
        "events": [],
    }
    return {
        "detail": individual,
        "pdfs": {
            document["id"]: pdf
            for document, pdf in [
                (w2, w2_pdf),
                (other, other_pdf),
                (intake, intake_pdf),
                (note, note_pdf),
            ]
        },
    }


def _business_scenario() -> dict[str, Any]:
    business_doc, business_pdf, business_cites = source(
        "studio-summary",
        "Sample Studio - demonstration summary",
        [
            "Business: Sample Studio LLC | Tax year: 2025",
            "Business address: 60 Example Way, Austin TX 78701",
            "Reported service receipts: 126000.00",
            "Reported other receipts: 1250.00",
            "These are demonstration bookkeeping figures, not tax elections or rules.",
        ],
    )
    receipts = field(
        "service-receipts",
        "Reported service receipts",
        "Bookkeeping summary",
        "money",
        [candidate("service-receipts", "summary", "126000.00", business_cites[2])],
    )
    other_receipts = field(
        "other-receipts",
        "Reported other receipts",
        "Bookkeeping summary",
        "money",
        [candidate("other-receipts", "summary", "1250.00", business_cites[3])],
    )
    business = {
        "id": BUSINESS_ID,
        "clientId": "sample-studio",
        "clientName": "Sample Studio LLC",
        "taxYear": 2025,
        "returnType": "business",
        "destination": "Business worksheet",
        "revision": 1,
        "activeFieldId": "service-receipts",
        "fields": [
            receipts,
            other_receipts,
            field(
                "total-receipts",
                "Total reported receipts",
                "Calculated",
                "money",
                [],
                value="127250.00",
                citations=receipts["citations"] + other_receipts["citations"],
                dependencies=["service-receipts", "other-receipts"],
                derivation="Reported service receipts + reported other receipts; bookkeeping demonstration only.",
            ),
            field(
                "business-address",
                "Business address",
                "Business details",
                "text",
                [
                    candidate(
                        "business-address",
                        "summary",
                        "60 Example Way, Austin TX 78701",
                        business_cites[1],
                    )
                ],
            ),
            field("contact-phone", "Contact phone", "Business details", "text", []),
        ],
        "documents": [business_doc],
        "events": [],
    }
    return {"detail": business, "pdfs": {business_doc["id"]: business_pdf}}


def _prior_year_scenario() -> dict[str, Any]:
    """Same client, independently seeded prior-year draft and source documents."""
    w2, w2_pdf, w2_citations = source(
        "morgan-2024-w2",
        "Morgan 2024 - W-2 example",
        [
            "Employee: Avery Morgan | Tax year: 2024",
            "Employer: Example Harbor Labs",
            "Box 1 wages: 68000.00",
            "Box 2 federal income tax withheld: 8300.00",
        ],
    )
    second_w2, second_pdf, second_citations = source(
        "morgan-2024-second-w2",
        "Morgan 2024 - second W-2 example",
        [
            "Employee: Avery Morgan | Tax year: 2024",
            "Employer: Example Evening School",
            "Box 1 wages: 6000.00",
        ],
    )
    intake, intake_pdf, intake_citations = source(
        "morgan-2024-intake",
        "Morgan 2024 - synthetic intake",
        [
            "Client: Avery Morgan | Tax year: 2024",
            "Mailing address: 14 Example Lane, Portland OR 97201",
            "Phone: not supplied",
        ],
    )
    primary = field(
        "primary-wages",
        "Primary W-2 wages",
        "Income",
        "money",
        [
            candidate("primary-wages", "original", "68000.00", w2_citations[2]),
        ],
    )
    withholding = field(
        "withholding",
        "Federal withholding",
        "Income",
        "money",
        [
            candidate("withholding", "original", "8300.00", w2_citations[3]),
        ],
    )
    secondary = field(
        "second-wages",
        "Second W-2 wages",
        "Income",
        "money",
        [
            candidate("second-wages", "original", "6000.00", second_citations[2]),
        ],
    )
    total = field(
        "total-wages",
        "Total wages",
        "Calculated",
        "money",
        [],
        value="74000.00",
        citations=primary["citations"] + secondary["citations"],
        dependencies=["primary-wages", "second-wages"],
        derivation="Primary W-2 wages + second W-2 wages (demonstration sum, not tax advice).",
    )
    address = field(
        "mailing-address",
        "Mailing address",
        "Client details",
        "text",
        [
            candidate(
                "mailing-address",
                "intake",
                "14 Example Lane, Portland OR 97201",
                intake_citations[1],
            ),
        ],
    )
    detail = {
        "id": PRIOR_YEAR_ID,
        "clientId": "avery-morgan",
        "clientName": "Avery Morgan",
        "taxYear": 2024,
        "returnType": "individual",
        "destination": "Individual worksheet",
        "revision": 1,
        "activeFieldId": "primary-wages",
        "fields": [
            primary,
            withholding,
            secondary,
            total,
            address,
            field("phone", "Phone number", "Client details", "text", []),
        ],
        "documents": [w2, second_w2, intake],
        "events": [],
    }
    return {
        "detail": detail,
        "pdfs": {w2["id"]: w2_pdf, second_w2["id"]: second_pdf, intake["id"]: intake_pdf},
    }


def legacy_scenarios() -> list[dict[str, Any]]:
    """Historical worksheet snapshots for non-destructive migration regressions."""
    return [_individual_scenario(), _business_scenario(), _prior_year_scenario()]


def scenarios() -> list[dict[str, Any]]:
    """Actual intake facts with their own literal evidence; historical W-2s stay archived."""
    result = []
    for scenario in legacy_scenarios():
        detail = migrate_return(scenario["detail"])
        business = detail["returnType"] == "business"
        values = {
            "full_name": detail["clientName"],
            "tax_year": str(detail["taxYear"]),
            "email": "office@example.test" if business else "avery@example.test",
            "street_address": "60 Example Way" if business else "14 Example Lane",
            "city": "Austin" if business else "Portland",
            "state": "TX" if business else "OR",
            "zip_code": "78701" if business else "97201",
            "employer": "Sample Studio LLC" if business else "Example Harbor Labs",
            "filing_status": "single",
            "number_of_dependents": "0",
            "income_sources": '["self_employment"]' if business else '["w2_wages"]',
            "urgency": "medium",
            "notes": "Please contact me by email.",
        }
        doc, pdf, citations = source(
            f"{detail['id']}-intake-facts",
            "Explicit synthetic client intake facts",
            [f"{name}: {value}" for name, value in values.items()],
        )
        detail["documents"].append(doc)
        scenario["pdfs"][doc["id"]] = pdf
        for (name, value), citation in zip(values.items(), citations):
            item = next(item for item in detail["fields"] if item["id"] == name)
            proposal = candidate(name, "intake", value, citation)
            item.update(
                value=value,
                valueOrigin="source",
                candidates=[proposal],
                citations=[citation],
                selectedCandidateId=proposal["id"],
                status="needs_review",
            )
        if not business and detail["taxYear"] == 2025:
            doc, pdf, citations = source(
                "morgan-street-note",
                "Morgan - explicit street correction",
                ["Street address: 28 Sample Avenue"],
            )
            detail["documents"].append(doc)
            scenario["pdfs"][doc["id"]] = pdf
            street = next(item for item in detail["fields"] if item["id"] == "street_address")
            street["candidates"].append(
                candidate("street_address", "client-note", "28 Sample Avenue", citations[0])
            )
            street.update(conflict=True, status="blocked", selectedCandidateId=None)
        detail["activeFieldId"] = "employer"
        result.append(scenario)
    return result


def seeded_store(data_dir) -> ReviewStore:
    store = ReviewStore(data_dir)
    populate_store(store)
    return store


def populate_store(store: ReviewStore) -> None:
    """Insert fixtures explicitly, without a production seed hook or sample table."""
    with sqlite3.connect(store.database_path) as connection:
        for scenario in scenarios():
            detail = scenario["detail"]
            connection.execute(
                "INSERT INTO review_returns (id, payload, updated_at) VALUES (?, ?, ?)",
                (detail["id"], json.dumps(detail), "2026-01-01T00:00:00+00:00"),
            )
            for document_id, pdf in scenario["pdfs"].items():
                connection.execute(
                    "INSERT INTO review_documents (return_id, document_id, pdf) VALUES (?, ?, ?)",
                    (detail["id"], document_id, pdf),
                )


def corrected_source() -> tuple[dict, bytes, list[dict]]:
    return source(
        "morgan-w2-corrected",
        "Morgan - corrected W-2 example",
        [
            "Employee: Avery Morgan | Tax year: 2025",
            "Employer: Example Harbor Labs | CORRECTED",
            "Box 1 wages: 74250.00",
            "Box 2 federal income tax withheld: 9475.00",
            "Synthetic supplementary evidence; original document retained.",
        ],
        version=2,
    )


def add_corrected_source(store: ReviewStore, detail: dict) -> dict:
    """Archive corrected W-2 evidence without inventing unsupported intake amounts."""
    _, pdf, _ = corrected_source()
    return store.add_document(detail["id"], detail["revision"], "corrected-w2.pdf", pdf)
