import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest

from src.preparation.agent import Authorization, DataClass, DraftAssignment, PreparationBlocked, StagePolicy, prepare
from src.preparation.providers import ProviderError, ProviderIdentity, ProviderTurn, ToolCall, Usage
from src.preparation.tool_contract import (
    Action, ActionOutcome, Choice, JobScope, MutationDetails, MutationPermit,
    Observation, Verification, VerifiedReadback,
)


IDENTITY = ProviderIdentity("gemini", "test-model", "https://example.test", "practice")
SCOPE = JobScope(job_id="job", owner_generation=1, profile_id="practice", draft_key="PRIVATE-DRAFT-ID")
CLASSES: frozenset[DataClass] = frozenset({"synthetic"})
ASSIGNMENT = DraftAssignment(
    SCOPE, "revision-1", CLASSES, frozenset({"w2"}), frozenset({"w2.wages"}),
    (("field-a", ("fact-a",)),), frozenset(), 1, 1,
)
POLICY = StagePolicy(IDENTITY, "reasoning", CLASSES, 1, 8192, 5, 512, 64, Decimal(1), Decimal(1), Decimal(1))
ACTION = Action(name="set_field", arguments={
    "observationId": "obs-1", "controlRef": "ref-a", "bindingId": "field-a", "factIds": ["fact-a"],
})
CHOICE = Choice(action=ACTION, semantic_key="w2.wages", mutation=MutationDetails(before=None, intended="9876.54"))
SAVE = Action(name="save_draft", arguments={"observationId": "obs-2", "scopeRef": "ref-draft"})
SAVE_CHOICE = Choice(action=SAVE, semantic_key="w2.wages", mutation=MutationDetails(before=None, intended="saved"))
NAVIGATE = Action(name="open_section", arguments={"observationId": "obs-2", "controlRef": "ref-section"})
NAV_CHOICE = Choice(action=NAVIGATE, semantic_key="w2.wages")


def observed(observation_id, **kwargs):
    return Observation(scope=SCOPE, observation_id=observation_id, context="w2", **kwargs)


class Practice:
    identity = IDENTITY

    def __init__(self, proposed=None, status="applied", *, observations=None, verification=None, usage=Usage(20, 10)):
        self.trace = []
        self.writes = 0
        self.proposed = proposed or ACTION
        self.status = status
        self.usage = usage
        self.observations = iter(observations if observations is not None else (
            observed("obs-1", choices=(CHOICE,)),
            observed("obs-2", completed_binding_ids=("field-a",)),
        ))
        self.verification = verification
        self.request_limits = []

    async def require_owner(self, scope):
        assert scope == SCOPE

    async def observe(self):
        self.trace.append("observe")
        return next(self.observations)

    async def propose(self, observation, tools, previous, *, max_output_tokens, max_request_bytes):
        self.trace.append("propose")
        self.request_limits.append((max_output_tokens, max_request_bytes))
        assert set(observation) == {"context", "observationId", "choices"}
        assert "PRIVATE-DRAFT-ID" not in str(observation)
        assert "9876.54" not in str(observation)
        if self.proposed == "none":
            return ProviderTurn((), self.usage)
        action = SAVE if self.writes else self.proposed
        args = action.arguments.model_dump(mode="json", by_alias=True)
        return ProviderTurn((ToolCall(f"call-{self.writes + 1}", action.name, args),), self.usage)

    async def begin(self, scope, choice):
        self.trace.append("journal")
        return MutationPermit(scope=scope, step_id="step-1", action=choice.action, details=choice.mutation)

    async def execute(self, action, permit):
        assert self.trace[-1] == "journal"
        assert permit.action == action
        self.trace.append("execute")
        self.writes += 1
        return ActionOutcome(status=self.status)

    async def readback(self, permit, outcome, observed):
        assert observed.observation_id != permit.action.observation_id
        self.trace.append("readback")

    async def start_verification(self):
        self.trace.append("start-verification")

    async def record_verification(self, result):
        assert result.scope == SCOPE
        self.trace.append("verified")

    async def verify(self):
        self.trace.append("reopen")
        return self.verification if self.verification is not None else Verification(
            scope=SCOPE, verified_binding_ids=("field-a",) if self.writes else (),
        )


def run(practice, *, grant=True, policy=POLICY, assignment=ASSIGNMENT, reserve_request=None):
    authorization = Authorization(
        assignment.revision, "reasoning", IDENTITY, CLASSES, datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    return asyncio.run(prepare(
        assignment, provider=practice, policy=policy, grant=authorization if grant else None,
        host=practice, journal=practice, reserve_request=reserve_request,
    ))


def test_mutation_journals_before_dispatch_and_reopens_at_exact_step_limit():
    practice = Practice(observations=(
        observed("obs-1", choices=(CHOICE,)),
        # Optional navigation stays visible even though the assigned work is done.
        observed("obs-2", choices=(NAV_CHOICE,), completed_binding_ids=("field-a",)),
    ))
    assert run(practice).verified_binding_ids == ("field-a",)
    assert practice.trace == [
        "observe", "propose", "journal", "execute", "observe", "readback",
        "start-verification", "reopen", "verified",
    ]


def test_gemini_503_retries_proposal_without_replaying_prior_write():
    class BusyModel(Practice):
        busy = True

        async def propose(self, observation, tools, previous, **limits):
            if previous is not None and self.busy:
                self.busy = False
                self.trace.append("http-503")
                raise ProviderError("temporarily unavailable", status=503)
            return await super().propose(observation, tools, previous, **limits)

    practice = BusyModel(observations=(
        observed("obs-1", choices=(CHOICE,)),
        observed("obs-2", choices=(SAVE_CHOICE,), pending_save=True),
        observed("obs-3", completed_binding_ids=("field-a",)),
    ))
    assert run(practice, assignment=replace(ASSIGNMENT, max_steps=2),
               policy=replace(POLICY, max_requests=3)).verified_binding_ids == ("field-a",)
    assert practice.trace == [
        "observe", "propose", "journal", "execute", "observe", "readback", "http-503",
        "propose", "journal", "execute", "observe", "readback", "start-verification", "reopen", "verified",
    ]


@pytest.mark.parametrize("premature", [False, True])
def test_finish_cannot_skip_fields_or_change_the_draft(premature):
    class FinishModel(Practice):
        async def propose(self, observation, tools, previous, **limits):
            if not premature and previous is None:
                return await super().propose(observation, tools, previous, **limits)
            self.trace.append("finish-proposed")
            if not premature:
                assert [tool.name for tool in tools] == ["finish_draft"]
                assert observation["preparationComplete"]
            return ProviderTurn((ToolCall("finish-call", "finish_draft", {}),), self.usage)

    assignment = replace(ASSIGNMENT, max_steps=2, confirm_completion=True)
    policy = replace(POLICY, max_requests=2)
    model = FinishModel()
    if premature:
        with pytest.raises(PreparationBlocked, match="invalid-model-action"):
            run(model, assignment=assignment, policy=policy)
        assert model.writes == 0
        assert "reopen" not in model.trace
    else:
        assert run(model, assignment=assignment, policy=policy).verified_binding_ids == ("field-a",)
        assert model.writes == 1
        assert model.trace.index("finish-proposed") < model.trace.index("reopen")


def test_model_cannot_use_another_fact_for_an_observed_control():
    practice = Practice(ACTION.model_copy(update={
        "arguments": ACTION.arguments.model_copy(update={"fact_ids": ("other-fact",)}),
    }))
    with pytest.raises(PreparationBlocked, match="stale-or-unauthorized-action"):
        run(practice)
    assert practice.trace == ["observe", "propose"]


def test_uncertain_mutation_leaves_intent_without_false_acknowledgement():
    practice = Practice(status="uncertain")
    with pytest.raises(PreparationBlocked, match="reconciliation-required"):
        run(practice)
    assert practice.trace == ["observe", "propose", "journal", "execute"]


def test_missing_grant_and_insufficient_budget_never_call_provider():
    practice = Practice()
    with pytest.raises(PreparationBlocked, match="model-authorization-required"):
        run(practice, grant=False)
    assert practice.trace == []
    with pytest.raises(PreparationBlocked, match="model-budget-exhausted"):
        run(practice, policy=replace(POLICY, max_spend=Decimal(0)))
    assert practice.trace == ["observe"]


def test_existing_completed_bindings_need_no_model_call():
    practice = Practice(
        observations=(observed("obs-2", choices=(NAV_CHOICE,), completed_binding_ids=("field-a",)),),
        verification=Verification(scope=SCOPE, verified_binding_ids=("field-a",)),
    )
    assert not run(practice).issues
    assert practice.trace == ["observe", "start-verification", "reopen", "verified"]


def test_pending_save_requires_another_owned_action_even_if_bindings_are_accounted_for():
    practice = Practice(observations=(
        observed("obs-1", choices=(CHOICE,)),
        observed("obs-2", choices=(SAVE_CHOICE,), completed_binding_ids=("field-a",), pending_save=True),
        observed("obs-3", completed_binding_ids=("field-a",)),
    ))

    async def reserve():
        practice.trace.append("reserve")

    result = run(
        practice, assignment=replace(ASSIGNMENT, max_steps=2),
        policy=replace(POLICY, max_requests=2, request_output_tokens=42),
        reserve_request=reserve,
    )
    assert not result.issues
    assert practice.request_limits == [(42, POLICY.max_request_bytes)] * 2
    assert practice.trace == [
        "observe", "reserve", "propose", "journal", "execute", "observe", "readback",
        "reserve", "propose", "journal", "execute", "observe", "readback",
        "start-verification", "reopen", "verified",
    ]


@pytest.mark.parametrize("observation, proposed, issue", [
    (observed("obs-1"), None, "incomplete-preparation"),
    (observed("obs-1", pending_save=True, completed_binding_ids=("field-a",)), None, "incomplete-preparation"),
    (observed("obs-1", choices=(CHOICE,)), "none", "one-action-required"),
    (observed("obs-1", completed_binding_ids=("other-field",)), None, "invalid-preparation-progress"),
])
def test_unaccounted_or_invalid_progress_cannot_be_verified(observation, proposed, issue):
    practice = Practice(proposed=proposed, observations=(observation,))
    with pytest.raises(PreparationBlocked, match=issue):
        run(practice)
    assert "start-verification" not in practice.trace


def test_blocked_field_verifies_known_values_but_cannot_be_declared_ready():
    practice = Practice(observations=(observed("obs-2", blocked_binding_ids=("field-a",)),))
    result = run(practice)
    assert result.verified_binding_ids == ()
    assert result.issues == ("missing-reopened-fields", "blocked-preparation-fields")
    assert practice.trace == ["observe", "start-verification", "reopen", "verified"]


def test_unresolved_source_issue_survives_successful_native_reopen():
    practice = Practice()
    result = run(practice, assignment=replace(ASSIGNMENT, review_issues=("unresolved-source-overlap",)))
    assert result.verified_binding_ids == ("field-a",)
    assert result.issues == ("unresolved-source-overlap",)
    assert practice.trace[-3:] == ["start-verification", "reopen", "verified"]


@pytest.mark.parametrize("binding_id, fact_ids", [
    ("other-field", ("fact-a",)), ("field-a", ("other-fact",)),
])
def test_unassigned_reopened_value_is_rejected_even_when_it_did_not_match(binding_id, fact_ids):
    practice = Practice(
        observations=(observed("obs-2", completed_binding_ids=("field-a",)),),
        verification=Verification(scope=SCOPE, readbacks=(VerifiedReadback(
            scope=SCOPE, section_key="w2", field_key="wages", binding_id=binding_id, fact_ids=fact_ids,
            value_kind="decimal", normalized_value="123.00", matches_intended=False,
        ),)),
    )
    with pytest.raises(PreparationBlocked, match="unexpected-reopened-field"):
        run(practice)
    assert practice.trace == ["observe", "start-verification", "reopen"]


def test_reservation_uses_billable_bound_and_does_not_round_down():
    practice = Practice()
    policy = replace(
        POLICY, max_input_tokens=1, max_output_tokens=1, request_output_tokens=1,
        input_price_per_million=Decimal("1.001"), output_price_per_million=Decimal(0),
        max_spend=Decimal("0.000001"),
    )
    with localcontext() as ctx:
        ctx.prec = 1  # Exact reservation still exceeds the configured maximum.
        with pytest.raises(PreparationBlocked, match="model-budget-exhausted"):
            run(practice, policy=policy)
    assert practice.trace == ["observe"]


def test_usage_outside_enrolled_bound_does_not_authorize_mutation():
    practice = Practice(usage=Usage(POLICY.max_input_tokens + 1, 1))
    with pytest.raises(PreparationBlocked, match="model-usage-outside-policy"):
        run(practice)
    assert practice.trace == ["observe", "propose"]


def test_policy_and_grant_round_trip_as_strict_credential_free_snapshots():
    policy = replace(
        POLICY, document_classes=frozenset({"synthetic", "approved_redacted"}), request_output_tokens=16,
        input_price_per_million=Decimal("1.234567890123456789"),
    )
    wire = policy.to_wire()
    assert wire["input_price_per_million"] == "1.234567890123456789"
    assert wire["document_classes"] == ["approved_redacted", "synthetic"]
    assert StagePolicy.from_wire(wire) == policy
    with pytest.raises(ValueError):
        StagePolicy.from_wire(wire | {"max_request_bytes": "8192"})
    with pytest.raises(ValueError):
        StagePolicy.from_wire(wire | {"input_price_per_million": 1.23})
    with pytest.raises(ValueError):
        StagePolicy.from_wire(wire | {"identity": wire["identity"] | {"api_key": "not-in-the-contract"}})
    grant = Authorization(
        ASSIGNMENT.revision, "reasoning", IDENTITY, CLASSES, datetime(2050, 1, 1, tzinfo=timezone.utc),
    )
    assert Authorization.from_wire(grant.to_wire()) == grant
    with pytest.raises(ValueError):
        Authorization.from_wire(grant.to_wire() | {"expires_at": "2050-01-01T00:00:00"})
