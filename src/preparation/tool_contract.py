"""Bounded tool messages shared by the controller and its local child process."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StrictInt, StringConstraints, model_validator
from pydantic.alias_generators import to_camel

Ref = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_.:-]{1,120}$")]
ActionName = Literal[
    "open_section", "open_detail", "list_records", "ensure_record", "set_field", "set_fields", "save_draft"
]


class ToolMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, alias_generator=to_camel, populate_by_name=True)


class JobScope(ToolMessage):
    job_id: Ref
    owner_generation: Annotated[StrictInt, Field(ge=1)]
    profile_id: Ref
    draft_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]


class ObservedArguments(ToolMessage):
    observation_id: Ref


class NavigateArguments(ObservedArguments):
    control_ref: Ref


class ListArguments(ObservedArguments):
    list_ref: Ref


class RecordArguments(ListArguments):
    source_record_id: Ref


class FieldReferences(ToolMessage):
    binding_id: Ref
    fact_ids: Annotated[tuple[Ref, ...], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def unique_facts(self):
        if len(set(self.fact_ids)) != len(self.fact_ids):
            raise ValueError("A binding cannot repeat a fact reference.")
        return self


class SetArguments(NavigateArguments, FieldReferences):
    pass


class SetFieldsArguments(ObservedArguments):
    fields: Annotated[tuple[FieldReferences, ...], Field(min_length=2, max_length=128)]

    @model_validator(mode="after")
    def unique_bindings(self):
        if len({field.binding_id for field in self.fields}) != len(self.fields):
            raise ValueError("A grouped write cannot repeat a field binding.")
        return self


class SaveArguments(ObservedArguments):
    scope_ref: Ref


ARGUMENT_TYPES = {
    "open_section": NavigateArguments,
    "open_detail": NavigateArguments,
    "list_records": ListArguments,
    "ensure_record": RecordArguments,
    "set_field": SetArguments,
    "set_fields": SetFieldsArguments,
    "save_draft": SaveArguments,
}


class Action(ToolMessage):
    name: ActionName
    arguments: NavigateArguments | RecordArguments | ListArguments | SetArguments | SetFieldsArguments | SaveArguments

    @model_validator(mode="before")
    @classmethod
    def parse_arguments(cls, value):
        if not isinstance(value, dict):
            return value
        name = value.get("name")
        if not isinstance(name, str) or name not in ARGUMENT_TYPES:
            raise ValueError("Unrecognized draft action.")
        return {**value, "arguments": ARGUMENT_TYPES[name].model_validate(value.get("arguments"))}

    @property
    def observation_id(self) -> str:
        return self.arguments.observation_id

    @property
    def mutates(self) -> bool:
        return self.name in {"ensure_record", "set_field", "set_fields", "save_draft"}


class MutationDetails(ToolMessage):
    """Local journal evidence. These states never enter provider requests."""

    before: JsonValue
    intended: JsonValue


class Choice(ToolMessage):
    action: Action
    semantic_key: Ref
    mutation: MutationDetails | None = None

    @model_validator(mode="after")
    def journal_evidence(self):
        if self.action.mutates != (self.mutation is not None):
            raise ValueError("Mutation choices must have exact before and intended states.")
        return self


class Observation(ToolMessage):
    scope: JobScope
    observation_id: Ref
    context: Ref = "draft"
    choices: Annotated[tuple[Choice, ...], Field(max_length=256)] = ()
    completed_binding_ids: tuple[Ref, ...] = ()
    blocked_binding_ids: tuple[Ref, ...] = ()
    pending_save: StrictBool = False

    @model_validator(mode="after")
    def scoped_choices(self):
        completed, blocked = set(self.completed_binding_ids), set(self.blocked_binding_ids)
        if (len(completed) != len(self.completed_binding_ids) or len(blocked) != len(self.blocked_binding_ids)
                or completed & blocked):
            raise ValueError("Binding progress must be unique and disjoint.")
        seen = set()
        for choice in self.choices:
            if choice.action.observation_id != self.observation_id:
                raise ValueError("Observation contains a stale choice.")
            key = choice.action.model_dump_json()
            if key in seen:
                raise ValueError("Observation contains an ambiguous choice.")
            seen.add(key)
        return self

    def authorizes(self, action: Action) -> bool:
        return self.choice_for(action) is not None

    def choice_for(self, action: Action) -> Choice | None:
        if action.observation_id != self.observation_id:
            return None
        return next((choice for choice in self.choices if choice.action == action), None)


class ActionOutcome(ToolMessage):
    status: Literal["applied", "unchanged", "uncertain"]


class VerifiedReadback(ToolMessage):
    """Independently reopened native value; local only, never provider or selection stream data."""

    scope: JobScope
    section_key: Ref
    field_key: Ref
    binding_id: Ref
    fact_ids: Annotated[tuple[Ref, ...], Field(min_length=1, max_length=32)]
    source_record_id: Ref | None = None
    native_row_key: Ref | None = None
    value_kind: Literal["decimal", "text"]
    # Invalid nonempty decimal text cannot be normalized; blank is known, not unknown.
    normalized_value: Annotated[str, StringConstraints(strict=True, max_length=4096)] | None = Field(repr=False)
    matches_intended: StrictBool

    @model_validator(mode="after")
    def exact_target(self):
        if ((self.source_record_id is None) != (self.native_row_key is None)
                or len(set(self.fact_ids)) != len(self.fact_ids)
                or (self.matches_intended and not self.normalized_value)
                or (self.value_kind == "text" and self.normalized_value is None)):
            raise ValueError("Readback must identify its exact scope and normalized value.")
        if self.value_kind == "decimal" and self.normalized_value:
            try:
                finite = Decimal(self.normalized_value).is_finite()
            except InvalidOperation:
                finite = False
            if not finite:
                raise ValueError("Normalized numeric readback must be finite decimal text.")
        return self


class Verification(ToolMessage):
    scope: JobScope
    verified_binding_ids: tuple[Ref, ...] = ()
    issues: tuple[Ref, ...] = ()
    readbacks: tuple[VerifiedReadback, ...] = ()

    @model_validator(mode="after")
    def scoped_readbacks(self):
        verified = set(self.verified_binding_ids)
        if len(verified) != len(self.verified_binding_ids):
            raise ValueError("Verification cannot duplicate binding references.")
        if self.readbacks and (
            any(item.scope != self.scope for item in self.readbacks)
            or len({item.binding_id for item in self.readbacks}) != len(self.readbacks)
            or len({(item.section_key, item.native_row_key, item.field_key)
                    for item in self.readbacks}) != len(self.readbacks)
            or verified != {item.binding_id for item in self.readbacks if item.matches_intended}
        ):
            raise ValueError("Readbacks must uniquely match the scoped verification.")
        return self


class MutationPermit(ToolMessage):
    scope: JobScope
    step_id: Ref
    action: Action
    details: MutationDetails
