"""Exact decoder for the generated Sigma practice originals; never use for live documents."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import json
import re

from src.preparation.contracts import (
    Amount, Candidate, ExactClaim, ExtractionIssue, Form, OriginalRef, RecordIdentity,
    SourceOccurrence, TaxExtraction, TextValue,
)
from src.preparation.evidence import LoadedOriginal, VerifiedFact

from .pdf import SYNTHETIC_FOOTER

SUBJECT_NAME = "Avery Morgan"
SUBJECT_REFERENCE = "TEST-PERSON-01"
TAX_YEAR = 2025

_IDENTITY = f"Employee: {SUBJECT_NAME} | Employee reference: {SUBJECT_REFERENCE} | Year: {TAX_YEAR}"
_MARKER = "SYNTHETIC DEMONSTRATION - NOT A FILED TAX DOCUMENT"
_MONEY = r"\$(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\.[0-9]{2}"
_EMPLOYER = r"Employer: ([A-Za-z ]+) \| Employer reference: (TEST-EMP-[A-Z]) \| Form instance: ([A-Z]-[0-9]+)"
_W2_FIELDS = {
    "Box 1 wages": "box-1",
    "Box 2 income tax withheld": "box-2",
    "Box 3 Social Security wages": "social_security_wages",
}
_PAYER = r"Payer: Example Ride \| Activity: (RIDESHARE-[0-9]{2})"
_RECEIPT = rf"Receipt (ROTOR-[0-9]+) \| ({TAX_YEAR}-[0-9]{{2}}-[0-9]{{2}}) \| ([A-Za-z ]+): ([A-Za-z ]+) \| ({_MONEY})"


class _Unrecognized(ValueError):
    pass


def _match(pattern: str, line: str) -> re.Match[str]:
    match = re.fullmatch(pattern, line)
    if match is None:
        raise _Unrecognized
    return match


def _identity(form: Form, issuer: str, instance: str, *, activity: str | None = None) -> RecordIdentity:
    return RecordIdentity(tax_year=TAX_YEAR, form=form, subject_reference=SUBJECT_REFERENCE,
                          issuer_reference=issuer, activity_reference=activity,
                          period=str(TAX_YEAR) if activity else None, instance_reference=instance)


def _decode(original: OriginalRef, pages: list[dict]) -> tuple[SourceOccurrence, ...]:
    if len(pages) != 1 or pages[0]["page"] not in (None, 1):
        raise _Unrecognized
    page = pages[0]["page"]
    numbered = pages[0]["lines"]
    if any(line["line"] != index for index, line in enumerate(numbered, 1)):
        raise _Unrecognized
    lines = [line["text"] for line in numbered]

    def candidate(key: str, index: int, raw: str, *, amount: bool = True) -> Candidate:
        value = Amount(kind="amount", value=raw[1:].replace(",", "")) if amount else TextValue(kind="text", value=raw)
        quote = ExactClaim(precision="exact", original=original, page=page,
                           line_start=index + 1, line_end=index + 1, quote=lines[index])
        return Candidate(fact_key=key, column="reported", raw_value=raw, normalized=value, citations=(quote,))

    def amount_row(key: str, index: int, label: str) -> Candidate:
        raw, = _match(rf"{re.escape(label)}: ({_MONEY})", lines[index]).groups()
        return candidate(key, index, raw)

    def occurrence(identity: RecordIdentity, *facts: Candidate) -> SourceOccurrence:
        return SourceOccurrence(identity=identity, original=original, candidates=facts)

    if page is not None:
        if len(lines) < 6 or lines[1:3] != [_MARKER, _IDENTITY] or lines[-1] != SYNTHETIC_FOOTER:
            raise _Unrecognized
        title = lines[0]
        if len(lines) in (7, 8) and title.startswith("Form W-2: "):
            employer, issuer, instance = _match(_EMPLOYER, lines[3]).groups()
            if title != f"Form W-2: {employer}":
                raise _Unrecognized
            fields = {}
            for index, row in enumerate(lines[4:-1], 4):
                label, separator, raw = row.partition(": ")
                key = _W2_FIELDS.get(label)
                if key is None or key in fields or not separator:
                    raise _Unrecognized
                _match(_MONEY, raw)
                fields[key] = candidate(key, index, raw)
            if not {"box-1", "box-2"} <= fields.keys():
                raise _Unrecognized
            return (occurrence(_identity("w2", issuer, instance), *fields.values()),)
        if len(lines) == 6 and title == "Form 1099-K: Example Ride":
            activity, instance = _match(_PAYER + r" \| Form instance: (K-[0-9]+)", lines[3]).groups()
            return (occurrence(_identity("1099k", "Example Ride", instance, activity=activity),
                               amount_row("gross-receipts", 4,
                                          "Box 1a gross payment card and third party network transactions")),)
        if (len(lines) == 7 and title == "Form 1099-NEC: Example Ride"
                and lines[5] == "Bonus allocation between payouts and gross fares not specified."):
            activity, instance = _match(_PAYER + r" \| Form instance: (NEC-[0-9]+)", lines[3]).groups()
            return (occurrence(_identity("1099nec", "Example Ride", instance, activity=activity),
                               amount_row("nonemployee-compensation", 4, "Box 1 nonemployee compensation")),)
    elif len(lines) >= 3 and lines[0] == _IDENTITY:
        if len(lines) == 5 and lines[1].startswith("Payer: "):
            activity, = _match(_PAYER + rf" \| Period: {TAX_YEAR}", lines[1]).groups()
            return (occurrence(_identity("platform_summary", "Example Ride", "annual-summary", activity=activity),
                               amount_row("gross-receipts", 2, "Gross fares (also reported on the 1099-K)"),
                               amount_row("platform-fees", 3, "Platform fees"),
                               amount_row("net-payout", 4, "Net payout")),)
        if 3 <= len(lines) <= 12:
            activity, = _match(r"Activity: (RIDESHARE-[0-9]{2})", lines[1]).groups()
            receipts = []
            seen = set()
            for index, row in enumerate(lines[2:], 2):
                reference, occurred, merchant, description, raw = _match(_RECEIPT, row).groups()
                if reference in seen:
                    raise _Unrecognized
                seen.add(reference)
                try:
                    date.fromisoformat(occurred)
                except ValueError as error:
                    raise _Unrecognized from error
                receipts.append(occurrence(_identity("receipt", merchant, reference, activity=activity),
                                           candidate("description", index, f"{merchant}: {description}", amount=False),
                                           candidate("amount", index, raw)))
            return tuple(receipts)
    raise _Unrecognized


def extract_numbered(source_text: str) -> TaxExtraction:
    """Decode only literal numbered source text; no filename, requested case, or target hints."""
    payload = json.loads(source_text)
    original = OriginalRef.model_validate(payload["original"])
    try:
        records = _decode(original, payload["pages"])
    except _Unrecognized:
        return TaxExtraction(schema_version=1, records=(),
                             issues=(ExtractionIssue(original=original, code="unknown_form"),))
    return TaxExtraction(schema_version=1, records=records, issues=())


def verify_document(document: LoadedOriginal) -> tuple[VerifiedFact, ...]:
    """Attest from the independently loaded stored version, without trusting extraction output."""
    try:
        occurrences = _decode(document.scope.original, document.numbered_pages())
    except _Unrecognized:
        return ()
    return tuple(VerifiedFact(scope=document.scope, identity=occurrence.identity, fact_key=fact.fact_key,
                              column=fact.column, raw_value=fact.raw_value, normalized=fact.normalized,
                              anchors=fact.citations)
                 for occurrence in occurrences for fact in occurrence.candidates)


def recognized_documents(facts: tuple[VerifiedFact, ...]) -> str:
    details: dict[Form, dict[str, None]] = defaultdict(dict)
    for fact in facts:
        identity = fact.identity
        detail = identity.instance_reference if identity.form == "receipt" else identity.issuer_reference
        if detail:
            details[identity.form][detail] = None
    names = {"w2": "W-2", "1099k": "1099-K", "1099nec": "1099-NEC",
             "platform_summary": "platform summary", "receipt": "receipts"}
    return "; ".join(f"{names[form]} ({', '.join(identities)})" for form, identities in details.items())
