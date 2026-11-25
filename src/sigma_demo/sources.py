"""Synthetic source originals for the Sigma practice cases. Never load in production."""

from dataclasses import dataclass

from .pdf import source


@dataclass(frozen=True, slots=True)
class PracticeSource:
    document_id: str
    filename: str
    media_type: str
    content: bytes


def _pdf(document_id: str, title: str, *rows: str) -> PracticeSource:
    _, original, _ = source(document_id, title, list(rows))
    return PracticeSource(document_id, f"{document_id}.pdf", "application/pdf", original)


def _txt(document_id: str, *rows: str) -> PracticeSource:
    return PracticeSource(
        document_id, f"{document_id}.txt", "text/plain", "\n".join((*rows, "")).encode()
    )


def individual_sources(*, include_social_security_wages: bool = False) -> tuple[PracticeSource, ...]:
    """Optionally include Box 3 on Harbor Labs' W-2 only."""
    identity = "Employee: Avery Morgan | Employee reference: TEST-PERSON-01 | Year: 2025"
    harbor_box_3 = ("Box 3 Social Security wages: $30,000.00",) if include_social_security_wages else ()
    return (
        _pdf(
            "example-w2-a", "Form W-2: Harbor Labs", identity,
            "Employer: Harbor Labs | Employer reference: TEST-EMP-A | Form instance: A-1",
            "Box 1 wages: $30,000.00", *harbor_box_3, "Box 2 income tax withheld: $1,600.00",
        ),
        _pdf(
            "example-w2-b", "Form W-2: Juniper Design", identity,
            "Employer: Juniper Design | Employer reference: TEST-EMP-B | Form instance: B-1",
            "Box 1 wages: $30,000.00", "Box 2 income tax withheld: $800.00",
        ),
        _pdf(
            "example-1099k", "Form 1099-K: Example Ride", identity,
            "Payer: Example Ride | Activity: RIDESHARE-01 | Form instance: K-1",
            "Box 1a gross payment card and third party network transactions: $6,200.00",
        ),
        _txt(
            "example-platform-summary", identity,
            "Payer: Example Ride | Activity: RIDESHARE-01 | Period: 2025",
            "Gross fares (also reported on the 1099-K): $6,200.00",
            "Platform fees: $600.00", "Net payout: $5,600.00",
        ),
        _pdf(
            "example-1099nec", "Form 1099-NEC: Example Ride", identity,
            "Payer: Example Ride | Activity: RIDESHARE-01 | Form instance: NEC-1",
            "Box 1 nonemployee compensation: $200.00",
            "Bonus allocation between payouts and gross fares not specified.",
        ),
        _txt(
            "example-expense-receipts", identity, "Activity: RIDESHARE-01",
            "Receipt ROTOR-102 | 2025-02-20 | North Auto: Supplies | $45.00",
            "Receipt ROTOR-841 | 2025-09-15 | North Auto: Supplies | $45.00",
        ),
    )


def correction_source() -> PracticeSource:
    return _pdf(
        "example-w2c-b", "Form W-2c: Juniper Design",
        "Employee: Avery Morgan | Employee reference: TEST-PERSON-01 | Year: 2025",
        "Employer: Juniper Design | Employer reference: TEST-EMP-B | Form instance: B-1",
        "Box 2 income tax withheld | Previously reported: $800.00 | Correct information: $0.00",
    )


def corporate_source() -> PracticeSource:
    return _txt(
        "example-corporate-ledger", "Company: Cedar Studio Inc | Company reference: TEST-CORP-01",
        "Tax year: 2025 | Entity: 1120 | Period: 2025", "Account: Other deductions",
        "Ledger entry D-101 | 2025-04-15 | North Office | Office supplies | $125.00",
        "Ledger entry D-222 | 2025-04-28 | Sample Telecom | Communications | $125.00",
    )
