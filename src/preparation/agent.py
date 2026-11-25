"""Application-owned draft preparation. A model proposes; it cannot execute a tool."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from .providers import (
    BeforeRequest, Provider, ProviderError, ProviderIdentity, ProviderRequestTooLarge, ToolDefinition, ToolFeedback, Usage,
)
from .tool_contract import (
    ARGUMENT_TYPES, Action, ActionOutcome, Choice, JobScope, MutationPermit, Observation, Verification,
)
from .tools import ToolBackend, ToolHostError

Stage = Literal["extraction", "reasoning"]
DataClass = Literal["synthetic", "approved_redacted"]
_Result = TypeVar("_Result", bound=BaseModel)


class PreparationBlocked(RuntimeError):
    """Stable issue code. No document contents or SDK exception bodies."""


@dataclass(frozen=True, slots=True)
class Authorization:
    """Validated grant from the assignment service, already scoped to the input revision."""

    revision: str
    stage: Stage
    identity: ProviderIdentity
    document_classes: frozenset[DataClass]
    expires_at: datetime

    __pydantic_config__ = ConfigDict(extra="forbid")

    def to_wire(self) -> dict:
        """Serialize a grant for a protected assignment snapshot."""
        if self.expires_at.utcoffset() is None:
            raise ValueError("Authorization expiry must include a timezone.")
        data = _AUTHORIZATION_WIRE.dump_python(self, mode="json")
        data["document_classes"] = sorted(self.document_classes)
        data["expires_at"] = self.expires_at.astimezone(timezone.utc).isoformat()
        return data

    @classmethod
    def from_wire(cls, payload: object) -> Authorization:
        grant = _AUTHORIZATION_WIRE.validate_json(json.dumps(payload, allow_nan=False), strict=True)
        if grant.expires_at.utcoffset() is None:
            raise ValueError("Authorization expiry must include a timezone.")
        return grant


@dataclass(frozen=True, slots=True)
class StagePolicy:
    """Enrolled upper bounds for the exact model, endpoint and allowed request modes.

    Token bounds cover all billable input and output, including hosted framing,
    tool continuation and hidden reasoning. Prices cover the highest applicable
    token rates. These are deployment prerequisites, not estimates or limits
    that a usage report can impose after sending.
    """

    __pydantic_config__ = ConfigDict(extra="forbid")
    identity: ProviderIdentity
    stage: Stage
    document_classes: frozenset[DataClass]
    max_requests: int
    max_request_bytes: int
    timeout_seconds: float
    max_input_tokens: int
    max_output_tokens: int
    input_price_per_million: Decimal
    output_price_per_million: Decimal
    max_spend: Decimal
    request_output_tokens: int | None = None

    @property
    def output_limit(self) -> int:
        return self.max_output_tokens if self.request_output_tokens is None else self.request_output_tokens

    def __post_init__(self) -> None:
        if self.stage not in {"reasoning", "extraction"} or not self.document_classes <= {
            "synthetic", "approved_redacted",
        }:
            raise ValueError("Invalid data processing policy.")
        for value in (self.max_requests, self.max_request_bytes, self.max_input_tokens, self.max_output_tokens):
            if type(value) is not int or value <= 0:
                raise ValueError("Request and token ceilings must be positive integers.")
        if type(self.output_limit) is not int or not 0 < self.output_limit <= self.max_output_tokens:
            raise ValueError("The requested output ceiling must fit the billable output bound.")
        if not 0 < self.timeout_seconds < float("inf"):
            raise ValueError("Invalid time ceiling.")
        for value in (self.input_price_per_million, self.output_price_per_million, self.max_spend):
            if not value.is_finite() or value < 0:
                raise ValueError("Invalid model cost ceiling.")

    def to_wire(self) -> dict:
        """Serialize the immutable policy, without credentials or float prices."""
        data = _POLICY_WIRE.dump_python(self, mode="json")
        data["document_classes"] = sorted(self.document_classes)
        return data

    @classmethod
    def from_wire(cls, payload: object) -> StagePolicy:
        if not isinstance(payload, dict) or any(
            not isinstance(payload.get(key), str)
            for key in ("input_price_per_million", "output_price_per_million", "max_spend")
        ):
            raise ValueError("Snapshot prices and spend must be decimal strings.")
        return _POLICY_WIRE.validate_json(json.dumps(payload, allow_nan=False), strict=True)


_AUTHORIZATION_WIRE = TypeAdapter(Authorization)
_POLICY_WIRE = TypeAdapter(StagePolicy)


class _Budget:
    def __init__(self, policy: StagePolicy):
        self.output_bound = policy.max_output_tokens
        self.input_bound = policy.max_input_tokens
        self.max_requests = policy.max_requests
        self.requests = 0
        self.max_spend = Fraction(policy.max_spend)
        self.spend = Fraction()
        self.input_rate = Fraction(policy.input_price_per_million) / 1_000_000
        self.output_rate = Fraction(policy.output_price_per_million) / 1_000_000
        self.reserved = self._cost(self.input_bound, self.output_bound)

    def _cost(self, input_tokens: int, output_tokens: int) -> Fraction:
        return input_tokens * self.input_rate + output_tokens * self.output_rate

    def reserve(self) -> None:
        if self.requests >= self.max_requests or self.spend + self.reserved > self.max_spend:
            raise PreparationBlocked("model-budget-exhausted")
        self.requests += 1
        self.spend += self.reserved

    def settle(self, usage: Usage) -> None:
        if (
            type(usage.input_tokens) is not int or type(usage.output_tokens) is not int
            or not 0 <= usage.input_tokens <= self.input_bound
            or not 0 <= usage.output_tokens <= self.output_bound
        ):
            raise PreparationBlocked("model-usage-outside-policy")
        self.spend += self._cost(usage.input_tokens, usage.output_tokens) - self.reserved


def _require_authorization(
    policy: StagePolicy, grant: Authorization | None, provider: Provider,
    stage: Stage, revision: str, data_classes: frozenset[DataClass],
) -> None:
    if (
        not grant or grant.stage != stage or policy.stage != stage
        or grant.revision != revision or grant.identity != policy.identity or provider.identity != policy.identity
        or grant.expires_at.tzinfo is None or grant.expires_at <= datetime.now(timezone.utc)
        or not data_classes or not data_classes <= policy.document_classes or not data_classes <= grant.document_classes
    ):
        raise PreparationBlocked("model-authorization-required")


async def extract_structured(
    *, provider: Provider, policy: StagePolicy, grant: Authorization | None,
    revision: str, data_classes: frozenset[DataClass], instructions: str,
    source_text: str, result_type: type[_Result], before_request: BeforeRequest | None = None,
) -> _Result:
    """Extract one validated result. Denial or failure never selects a second provider."""
    _require_authorization(policy, grant, provider, "extraction", revision, data_classes)
    budget = _Budget(policy)
    schema = result_type.model_json_schema()
    budget.reserve()
    dispatched = False

    async def authorize_dispatch() -> None:
        nonlocal dispatched
        if dispatched:
            budget.reserve()
        _require_authorization(policy, grant, provider, "extraction", revision, data_classes)
        if before_request is not None:
            await before_request()
        _require_authorization(policy, grant, provider, "extraction", revision, data_classes)
        dispatched = True

    try:
        async with asyncio.timeout(policy.timeout_seconds):
            result, usage = await provider.extract(
                instructions, source_text, schema, max_output_tokens=policy.output_limit,
                max_request_bytes=policy.max_request_bytes, before_request=authorize_dispatch,
            )
    except TimeoutError:
        raise PreparationBlocked("model-timeout") from None
    except ProviderRequestTooLarge:
        raise PreparationBlocked("model-request-too-large") from None
    except ProviderError:
        raise PreparationBlocked("provider-unavailable") from None
    budget.settle(usage)
    try:
        return result_type.model_validate(result)
    except ValidationError:
        raise PreparationBlocked("invalid-extraction") from None


class OwnershipJournal(Protocol):
    async def require_owner(self, scope: JobScope) -> None:
        """Reject expired/revoked ownership. An observed timeout never grants a new writer."""
        ...

    async def begin(self, scope: JobScope, choice: Choice) -> MutationPermit:
        """Commit intent and preconditions atomically under the current grant before returning."""
        ...

    async def readback(self, permit: MutationPermit, outcome: ActionOutcome, observed: Observation) -> None:
        """Record immediate readback as distinct from persistence verified on reopen."""
        ...

    async def start_verification(self) -> None:
        """Mark the owned job verifying before its independent native reopen."""
        ...

    async def record_verification(self, verification: Verification) -> None:
        """Record reopened evidence while ownership is still held."""
        ...


@dataclass(frozen=True, slots=True)
class DraftAssignment:
    scope: JobScope
    revision: str
    document_classes: frozenset[DataClass]
    allowed_contexts: frozenset[str]
    allowed_semantic_keys: frozenset[str]
    # Only these job-local references and exact fact tuples may reach field write tools.
    binding_facts: tuple[tuple[str, tuple[str, ...]], ...]
    source_record_ids: frozenset[str]
    max_steps: int
    max_repeats: int
    review_issues: tuple[str, ...] = ()
    confirm_completion: bool = False


_DESCRIPTIONS = {
    "open_section": "Open the permitted section.",
    "open_detail": "Open the permitted nested detail or existing record.",
    "list_records": "Inspect the complete list of existing permitted records.",
    "ensure_record": "Match or create the assigned source record.",
    "set_field": "Set a field from its assigned facts, resolved locally.",
    "set_fields": "Enter the offered group of verified fields on the current page or record in one call.",
    "save_draft": "Save the observed draft or child scope.",
}
_TOOLS = {
    name: ToolDefinition(name, _DESCRIPTIONS[name], arguments.model_json_schema())
    for name, arguments in ARGUMENT_TYPES.items()
}
_FINISH = ToolDefinition(
    "finish_draft", "End draft preparation and hand off to independent save verification.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)


def _safe_observation(
    observation: Observation, assignment: DraftAssignment, bindings: dict[str, tuple[str, ...]],
) -> dict:
    if observation.scope != assignment.scope:
        raise PreparationBlocked("target-identity-changed")
    if observation.context not in assignment.allowed_contexts:
        raise PreparationBlocked("unrecognized-draft-context")
    public_choices = []
    for choice in observation.choices:
        if choice.semantic_key not in assignment.allowed_semantic_keys:
            raise PreparationBlocked("unrecognized-draft-control")
        action = choice.action
        arguments = action.arguments
        if action.name == "set_field" and bindings.get(arguments.binding_id) != arguments.fact_ids:
            raise PreparationBlocked("unassigned-tax-facts")
        if action.name == "set_fields" and (
            choice.semantic_key != observation.context
            or any(bindings.get(field.binding_id) != field.fact_ids for field in arguments.fields)
        ):
            raise PreparationBlocked("unassigned-tax-facts")
        if action.name == "ensure_record" and arguments.source_record_id not in assignment.source_record_ids:
            raise PreparationBlocked("unassigned-source-record")
        public_choices.append({**action.model_dump(mode="json", by_alias=True), "field": choice.semantic_key})
    # Scope and actual native values are local; public refs are generated by the scoped backend.
    return {"context": observation.context, "observationId": observation.observation_id, "choices": public_choices}


def _progress(observation: Observation, bindings: dict[str, tuple[str, ...]]) -> bool:
    completed = set(observation.completed_binding_ids)
    blocked = set(observation.blocked_binding_ids)
    accounted = completed | blocked
    if completed & blocked or not accounted <= bindings.keys():
        raise PreparationBlocked("invalid-preparation-progress")
    return not observation.pending_save and accounted == bindings.keys()


def _verify_assignment(
    verification: Verification, observation: Observation, bindings: dict[str, tuple[str, ...]],
    review_issues: tuple[str, ...],
) -> Verification:
    matched = set(verification.verified_binding_ids)
    if not matched <= bindings.keys() or any(
        bindings.get(item.binding_id) != item.fact_ids for item in verification.readbacks
    ):
        raise PreparationBlocked("unexpected-reopened-field")
    issues = list(dict.fromkeys((*verification.issues, *review_issues)))
    if matched != bindings.keys() and "missing-reopened-fields" not in issues:
        issues.append("missing-reopened-fields")
    if observation.blocked_binding_ids and "blocked-preparation-fields" not in issues:
        issues.append("blocked-preparation-fields")
    if tuple(issues) == verification.issues:
        return verification
    return verification.model_copy(update={"issues": tuple(issues)})


async def prepare(
    assignment: DraftAssignment, *, provider: Provider, policy: StagePolicy, grant: Authorization | None,
    host: ToolBackend, journal: OwnershipJournal, reserve_request: BeforeRequest | None = None,
) -> Verification:
    """Observe, propose once, validate, perform and read back before the next turn."""
    _require_authorization(policy, grant, provider, "reasoning", assignment.revision, assignment.document_classes)
    bindings = dict(assignment.binding_facts)
    if len(bindings) != len(assignment.binding_facts) or min(assignment.max_steps, assignment.max_repeats) < 1:
        raise PreparationBlocked("invalid-assignment")

    budget = _Budget(policy)
    repeats: Counter[tuple[str, str, str]] = Counter()
    call_ids: set[str] = set()
    previous = None

    try:
        async with asyncio.timeout(policy.timeout_seconds):
            await journal.require_owner(assignment.scope)
            observation = await host.observe()
            visible = _safe_observation(observation, assignment, bindings)
            for steps in range(assignment.max_steps + 1):
                complete = _progress(observation, bindings)
                if complete and not assignment.confirm_completion:
                    break
                if not complete and not observation.choices:
                    raise PreparationBlocked("incomplete-preparation")
                if steps == assignment.max_steps:
                    raise PreparationBlocked("step-budget-exhausted")
                if complete:
                    tools = (_FINISH,)
                    visible = {**visible, "choices": [{"name": _FINISH.name, "arguments": {}, "field": "draft"}],
                               "preparationComplete": True, "reviewIssues": assignment.review_issues}
                else:
                    tools = tuple(_TOOLS[name] for name in dict.fromkeys(c.action.name for c in observation.choices))
                for delay in (0, 1, 3):
                    if delay:
                        await asyncio.sleep(delay)
                        await journal.require_owner(assignment.scope)
                    _require_authorization(
                        policy, grant, provider, "reasoning", assignment.revision, assignment.document_classes,
                    )
                    budget.reserve()
                    if reserve_request is not None:
                        await reserve_request()
                        _require_authorization(
                            policy, grant, provider, "reasoning", assignment.revision, assignment.document_classes,
                        )
                    try:
                        turn = await provider.propose(
                            visible, tools, previous, max_output_tokens=policy.output_limit,
                            max_request_bytes=policy.max_request_bytes,
                        )
                    except ProviderError as error:
                        if provider.identity.provider != "gemini" or error.status != 503:
                            raise
                        if delay == 3:
                            raise PreparationBlocked("gemini-service-unavailable") from None
                        continue
                    break
                budget.settle(turn.usage)
                if len(turn.calls) != 1:
                    raise PreparationBlocked("one-action-required")
                call, = turn.calls
                if not call.call_id or call.call_id in call_ids:
                    raise PreparationBlocked("duplicate-model-call")
                call_ids.add(call.call_id)
                if complete:
                    if call.name != _FINISH.name or call.arguments != {}:
                        raise PreparationBlocked("invalid-completion")
                    break
                try:
                    action = Action(name=call.name, arguments=call.arguments)
                except ValidationError:
                    raise PreparationBlocked("invalid-model-action") from None
                choice = observation.choice_for(action)
                if choice is None:
                    raise PreparationBlocked("stale-or-unauthorized-action")

                key = observation.context, choice.semantic_key, action.name
                repeats[key] += 1
                if repeats[key] > assignment.max_repeats:
                    raise PreparationBlocked("repeated-action")
                await journal.require_owner(assignment.scope)
                permit = None
                if action.mutates:
                    permit = await journal.begin(assignment.scope, choice)
                    if permit.scope != assignment.scope or permit.action != action or permit.details != choice.mutation:
                        raise PreparationBlocked("invalid-mutation-permit")
                    await journal.require_owner(assignment.scope)
                outcome = await host.execute(action, permit)
                if outcome.status == "uncertain":
                    raise PreparationBlocked("reconciliation-required")
                await journal.require_owner(assignment.scope)
                observation = await host.observe()
                visible = _safe_observation(observation, assignment, bindings)
                if permit is not None:
                    await journal.readback(permit, outcome, observation)
                if outcome.status == "applied":
                    repeats[key] = 0
                previous = ToolFeedback(call, {"status": outcome.status})

            await journal.require_owner(assignment.scope)
            await journal.start_verification()
            verified = await host.verify()
            if verified.scope != assignment.scope:
                raise PreparationBlocked("target-identity-changed")
            result = _verify_assignment(verified, observation, bindings, assignment.review_issues)
            await journal.record_verification(result)
            return result
    except TimeoutError:
        raise PreparationBlocked("preparation-timeout") from None
    except ProviderRequestTooLarge:
        raise PreparationBlocked("model-request-too-large") from None
    except ProviderError:
        raise PreparationBlocked("provider-unavailable") from None
    except ToolHostError as error:
        raise PreparationBlocked(str(error)) from None
