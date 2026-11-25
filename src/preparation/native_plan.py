"""Local-only values and preconditions for a verified, unfiled destination."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import Field, StrictInt, StringConstraints, model_validator

from .contracts import Contract, Text
from .tool_contract import Ref

NativeValue = Annotated[str, StringConstraints(strict=True, max_length=2048)]


class DraftIdentity(Contract):
    client_reference: Text
    tax_year: Annotated[StrictInt, Field(ge=1900, le=2200)]
    return_type: Literal["individual", "business", "1120"]


class FieldPlan(Contract):
    field_key: Ref
    value: Text
    binding_id: Ref
    fact_ids: Annotated[tuple[Ref, ...], Field(min_length=1, max_length=32)]
    expected_saved: NativeValue | None = None
    preparer_owned: bool = False
    kind: Literal["decimal", "text"] = "text"

    @model_validator(mode="after")
    def validate_input(self):
        if not self.value.strip() or len(set(self.fact_ids)) != len(self.fact_ids):
            raise ValueError("A planned field requires a value and unique fact references.")
        if self.kind == "decimal" and not self._decimal_equal(self.value, self.value):
            raise ValueError("A decimal assignment requires a finite decimal value.")
        if self.expected_saved and self.kind == "decimal" and not self._decimal_equal(self.expected_saved, self.expected_saved):
            raise ValueError("A decimal baseline requires a finite decimal value.")
        return self

    @staticmethod
    def _decimal_equal(left: str, right: str) -> bool:
        try:
            lhs, rhs = Decimal(left), Decimal(right)
        except InvalidOperation:
            return False
        return lhs.is_finite() and rhs.is_finite() and lhs == rhs

    def _equal(self, actual: str, expected: str) -> bool:
        if self.kind == "decimal" and actual and expected:
            return self._decimal_equal(actual, expected)
        return actual == expected

    def matches(self, current: str) -> bool:
        return self._equal(current, self.value)

    def may_enter(self, current: str) -> bool:
        # An absent historical baseline permits a known native blank only, not an
        # unobserved field. Existing values need a matching prior verified save.
        baseline = "" if self.expected_saved is None else self.expected_saved
        return not self.preparer_owned and not self.matches(current) and self._equal(current, baseline)


class RecordPlan(Contract):
    source_record_id: Ref
    identity: Annotated[dict[Ref, Text], Field(min_length=1, max_length=10)]
    fields: Annotated[tuple[FieldPlan, ...], Field(max_length=128)]
    external_key: Ref | None = None
    allow_create: bool = False

    @model_validator(mode="after")
    def validate_row_fields(self):
        keys = {field.field_key for field in self.fields}
        if len(keys) != len(self.fields) or keys & self.identity.keys():
            raise ValueError("Native row identity cannot be an editable binding.")
        if self.external_key is not None and self.allow_create:
            raise ValueError("An existing bound row cannot be recreated.")
        return self


class SectionPlan(Contract):
    section_key: Ref
    fields: Annotated[tuple[FieldPlan, ...], Field(max_length=128)] = ()
    records: Annotated[tuple[RecordPlan, ...], Field(max_length=256)] = ()

    @model_validator(mode="after")
    def validate_section_assignment(self):
        if len({field.field_key for field in self.fields}) != len(self.fields):
            raise ValueError("A section cannot assign a field twice.")
        business_ids = {tuple(sorted(record.identity.items())) for record in self.records}
        external_keys = [record.external_key for record in self.records if record.external_key is not None]
        if len(business_ids) != len(self.records) or len(set(external_keys)) != len(external_keys):
            raise ValueError("A section cannot assign the same native record twice.")
        return self


class DraftPlan(Contract):
    identity: DraftIdentity
    sections: Annotated[tuple[SectionPlan, ...], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def validate_draft_assignment(self):
        bindings = [field.binding_id for section in self.sections for field in section.fields]
        source_records = []
        for section in self.sections:
            for record in section.records:
                source_records.append(record.source_record_id)
                bindings.extend(field.binding_id for field in record.fields)
        if (
            len({section.section_key for section in self.sections}) != len(self.sections)
            or len(set(bindings)) != len(bindings) or len(set(source_records)) != len(source_records)
        ):
            raise ValueError("Draft plan has ambiguous section, binding or record references.")
        return self
