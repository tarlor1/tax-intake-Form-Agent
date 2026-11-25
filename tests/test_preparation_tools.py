"""Exercise the actual MCP v2 stdio connection with a synthetic child."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from mcp.client import ClientSession
from mcp_types import CallToolResult
import pytest

from preparation_stdio_child import MUST_NOT_INHERIT, SCOPE, save_action
from src.preparation.tool_contract import MutationPermit, Observation
from src.preparation.tools import ToolHostClient, ToolHostError, open_stdio


CHILD = Path(__file__).resolve().with_name("preparation_stdio_child.py")


def journaled(choice, step_id):
    return MutationPermit(scope=SCOPE, step_id=step_id, action=choice.action, details=choice.mutation)


def test_stdio_filters_environment_and_admits_only_current_journaled_actions(monkeypatch, tmp_path):
    for key in MUST_NOT_INHERIT:
        monkeypatch.setenv(key, "synthetic-secret")
    monkeypatch.setenv("PATH", str(tmp_path / "untrusted-bin"))

    async def check():
        async with open_stdio(CHILD, SCOPE) as host:
            observation = await host.observe()
            assert observation.scope == SCOPE
            choice = observation.choices[0]
            action = choice.action
            permit = journaled(choice, "step-save")
            wrong_owner = permit.model_copy(update={"scope": SCOPE.model_copy(update={"owner_generation": 1})})
            with pytest.raises(ToolHostError, match="^missing-permit$"):
                await host.execute(action, None)
            with pytest.raises(ToolHostError, match="^scope-mismatch$"):
                await host.execute(action, wrong_owner)
            with pytest.raises(ToolHostError, match="^unauthorized-action$"):
                await host.execute(save_action(action.observation_id, "other-control"), permit)
            # A correct proposal can still become stale before native dispatch.
            with pytest.raises(ToolHostError, match="^stale-observation$"):
                await host.execute(action, permit)

            current = await host.observe()
            with pytest.raises(ToolHostError, match="^stale-observation$"):
                await host.execute(action, permit)
            current_choice = current.choices[0]
            assert (await host.execute(current_choice.action, journaled(current_choice, "step-save"))).status == "applied"

            latest = await host.observe()
            with pytest.raises(ToolHostError, match="^duplicate-step$"):
                await host.execute(latest.choices[0].action, journaled(latest.choices[0], "step-save"))
            assert (await host.verify()).verified_binding_ids == ("binding-1",)

    asyncio.run(check())


def test_stdio_refuses_a_different_draft_at_startup():
    async def check():
        with pytest.raises(ToolHostError, match="^scope-mismatch$"):
            async with open_stdio(CHILD, SCOPE.model_copy(update={"draft_key": "draft-2"})):
                pytest.fail("The child accepted an unverified draft")

    asyncio.run(check())


def test_stdio_does_not_continue_writing_after_uncertain_outcome():
    async def check():
        async with open_stdio(CHILD, SCOPE) as host:
            first = await host.observe()
            with pytest.raises(ToolHostError, match="^stale-observation$"):
                await host.execute(first.choices[0].action, journaled(first.choices[0], "step-uncertain"))
            ready = await host.observe()
            choice = ready.choices[0]
            assert (await host.execute(choice.action, journaled(choice, "step-uncertain"))).status == "uncertain"
            latest = await host.observe()
            with pytest.raises(ToolHostError, match="^reconciliation-required$"):
                await host.execute(latest.choices[0].action, journaled(latest.choices[0], "step-retry"))
            assert (await host.verify()).verified_binding_ids == ("binding-1",)

    asyncio.run(check())


def test_client_checks_result_shapes_and_response_scope():
    async def check():
        observation = Observation(scope=SCOPE, observation_id="observation-1")
        session = AsyncMock(spec=ClientSession)
        session.call_tool.side_effect = [
            CallToolResult(content=[], structured_content={"scope": SCOPE.model_dump(mode="json"), "observation_id": 1}),
            CallToolResult(content=[], structured_content=observation.model_dump(mode="json", by_alias=True)),
            CallToolResult(content=[], structured_content={
                "scope": SCOPE.model_dump(mode="json", by_alias=True), "outcome": {"status": "invented"},
            }),
            CallToolResult(content=[], structured_content={
                "scope": SCOPE.model_copy(update={"job_id": "job-2"}).model_dump(mode="json", by_alias=True),
            }),
        ]
        host = ToolHostClient(session, SCOPE)
        with pytest.raises(ToolHostError, match="^invalid-response$"):
            await host.observe()
        assert await host.observe() == observation
        with pytest.raises(ToolHostError, match="^invalid-response$"):
            action = save_action("observation-1")
            permit = MutationPermit(
                scope=SCOPE, step_id="step-save", action=action,
                details={"before": {"saved": False}, "intended": {"saved": True}},
            )
            await host.execute(action, permit)
        with pytest.raises(ToolHostError, match="^scope-mismatch$"):
            await host.verify()

    asyncio.run(check())


def test_client_rejects_a_second_in_flight_command():
    async def check():
        request_started = asyncio.Event()
        release = asyncio.Event()
        observation = Observation(scope=SCOPE, observation_id="observation-1")
        session = AsyncMock(spec=ClientSession)

        async def call_tool(name, arguments):
            request_started.set()
            await release.wait()
            return CallToolResult(content=[], structured_content=observation.model_dump(mode="json", by_alias=True))

        session.call_tool.side_effect = call_tool
        host = ToolHostClient(session, SCOPE)
        async with asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(host.observe())
            await request_started.wait()
            try:
                with pytest.raises(ToolHostError, match="^busy$"):
                    await host.verify()
            finally:
                release.set()
        assert pending.result() == observation
        session.call_tool.assert_awaited_once()

    asyncio.run(check())
