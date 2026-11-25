"""Versioned extraction and evidence contracts, independent of destination controls."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints, field_validator, model_validator
from pydantic.alias_generators import to_camel

from .tool_contract import Ref

Positive = Annotated[StrictInt, Field(ge=1)]
Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2048)]
Quote = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=20_000)]
Form = Literal["w2", "w2c", "1099k", "1099nec", "1099_other", "platform_summary", "receipt", "business_ledger"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, alias_generator=to_camel)


class OriginalRef(Contract):
    document_id: Ref
    version_id: Ref


class ExactClaim(Contract):
    precision: Literal["exact"]
    original: OriginalRef
    page: Positive | None
    line_start: Positive
    line_end: Positive
    quote: Quote

    @model_validator(mode="after")
    def ordered_lines(self):
        if self.line_end < self.line_start:
            raise ValueError("Source lines must be ordered.")
        return self


class PageEvidence(Contract):
    precision: Literal["page"]
    original: OriginalRef
    page: Positive


class DocumentEvidence(Contract):
    precision: Literal["document"]
    original: OriginalRef


class RegionEvidence(Contract):
    precision: Literal["region"]
    original: OriginalRef
    page: Positive
    rect: tuple[float, float, float, float]
    quote: Quote

    @field_validator("rect")
    @classmethod
    def inside_page(cls, rect):
        left, top, width, height = rect
        if not (0 <= left < 1 and 0 <= top < 1 and 0 < width <= 1 and 0 < height <= 1
                and left + width <= 1.000000001 and top + height <= 1.000000001):
            raise ValueError("Source rectangle is outside the page.")
        return rect


class LineEvidence(Contract):
    precision: Literal["lines"]
    original: OriginalRef
    line_start: Positive
    line_end: Positive
    quote: Quote

    @model_validator(mode="after")
    def ordered_lines(self):
        if self.line_end < self.line_start:
            raise ValueError("Source lines must be ordered.")
        return self


SourceClaim = Annotated[ExactClaim | PageEvidence | DocumentEvidence, Field(discriminator="precision")]
Evidence = Annotated[RegionEvidence | LineEvidence | PageEvidence | DocumentEvidence, Field(discriminator="precision")]


class Amount(Contract):
    kind: Literal["amount"]
    # JSON floats lose precision before validation; extraction must use decimal strings.
    value: Annotated[Decimal, Field(max_digits=24, decimal_places=6, allow_inf_nan=False)]

    @field_validator("value", mode="before")
    @classmethod
    def reject_binary_float(cls, value):
        if not isinstance(value, (str, Decimal)):
            raise ValueError("Amounts must use decimal strings.")
        return value


class BooleanValue(Contract):
    kind: Literal["boolean"]
    value: StrictBool


class TextValue(Contract):
    kind: Literal["text"]
    value: Text


class MissingValue(Contract):
    kind: Literal["missing", "not_applicable"]


FactValue = Annotated[Amount | BooleanValue | TextValue | MissingValue, Field(discriminator="kind")]


class RecordIdentity(Contract):
    tax_year: Annotated[StrictInt, Field(ge=1900, le=2200)]
    form: Form
    subject_reference: Text
    issuer_reference: Text | None
    activity_reference: Text | None
    period: Text | None
    instance_reference: Text


class Candidate(Contract):
    fact_key: Ref
    column: Literal["reported", "previously_reported", "correct_information"]
    raw_value: Text
    normalized: FactValue
    citations: Annotated[tuple[SourceClaim, ...], Field(min_length=1, max_length=16)]


class SourceOccurrence(Contract):
    identity: RecordIdentity
    original: OriginalRef
    candidates: Annotated[tuple[Candidate, ...], Field(max_length=128)]

    @model_validator(mode="after")
    def validate_occurrence(self):
        columns = {"previously_reported", "correct_information"} if self.identity.form == "w2c" else {"reported"}
        for candidate in self.candidates:
            if candidate.column not in columns or any(c.original != self.original for c in candidate.citations):
                raise ValueError("Candidate is outside its source occurrence.")
        keys = [(candidate.fact_key, candidate.column) for candidate in self.candidates]
        if len(set(keys)) != len(keys):
            raise ValueError("A source occurrence cannot report one box and column twice.")
        return self


class ExtractionIssue(Contract):
    original: OriginalRef
    code: Literal["unknown_form", "unreadable", "unassigned", "unverified_identity", "unverified_box"]


class TaxExtraction(Contract):
    schema_version: Literal[1]
    records: Annotated[tuple[SourceOccurrence, ...], Field(max_length=512)]
    issues: Annotated[tuple[ExtractionIssue, ...], Field(max_length=512)]
