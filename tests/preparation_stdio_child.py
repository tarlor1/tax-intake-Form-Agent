"""Synthetic child process: no browser, credentials, files, or network service."""

import asyncio
import os

from src.preparation.tool_contract import (
    Action,
    ActionOutcome,
    Choice,
    JobScope,
    MutationDetails,
    MutationPermit,
    Observation,
    Verification,
)
from src.preparation.tools import serve_stdio

SCOPE = JobScope(job_id="job-1", owner_generation=2, profile_id="practice", draft_key="draft-1")
MUST_NOT_INHERIT = (
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "AWS_SESSION_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "FIRM_QUEUE_TOKEN", "PYTHONPATH",
)


def save_action(observation_id: str, scope_ref: str = "draft-scope") -> Action:
    return Action(name="save_draft", arguments={"observationId": observation_id, "scopeRef": scope_ref})


class PracticeBackend:
    def __init__(self) -> None:
        self._reads = 0
        self._writes: list[str] = []

    async def observe(self) -> Observation:
        assert all(key not in os.environ for key in MUST_NOT_INHERIT)
        assert "untrusted-bin" not in os.environ.get("PATH", "")
        self._reads += 1
        # Change preconditions on the host's first pre-dispatch observation, even though the
        # displayed control and its native observation ID have not changed.
        observation_id = "observation-1" if self._reads <= 3 else f"observation-{len(self._writes) + 2}"
        return Observation(
            scope=SCOPE,
            observation_id=observation_id,
            choices=(Choice(
                action=save_action(observation_id),
                semantic_key="draft.save",
                mutation=MutationDetails(before={"revision": 2 if self._reads >= 3 else 1}, intended={"saved": True}),
            ),),
        )

    async def execute(self, action: Action, permit: MutationPermit | None) -> ActionOutcome:
        assert permit is not None and permit.scope == SCOPE and action == save_action(action.observation_id)
        self._writes.append(f"binding-{len(self._writes) + 1}")
        return ActionOutcome(status="uncertain" if permit.step_id == "step-uncertain" else "applied")

    async def verify(self) -> Verification:
        return Verification(scope=SCOPE, verified_binding_ids=tuple(self._writes))


if __name__ == "__main__":
    asyncio.run(serve_stdio(PracticeBackend()))
