"""Private MCP transport between the preparation controller and one tool backend."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys
from typing import Protocol, TypeVar

import anyio
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp_types as mcp
from pydantic import BaseModel, ConfigDict, ValidationError

from .tool_contract import Action, ActionOutcome, JobScope, MutationPermit, Observation, Verification


class ToolBackend(Protocol):
    """A trusted, scoped backend that checks native context again at dispatch.

    Observation IDs stay stable only while the native context and all action
    preconditions are unchanged. Verification is a read-only reopen/check.
    """

    async def observe(self) -> Observation: ...
    async def execute(self, action: Action, permit: MutationPermit | None) -> ActionOutcome: ...
    async def verify(self) -> Verification: ...


class ToolHostError(RuntimeError):
    """A safe status code, not a native exception or raw tool output."""


class _EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ExecuteRequest(_EmptyRequest):
    action: Action
    permit: MutationPermit | None = None


class _ScopedOutcome(_EmptyRequest):
    scope: JobScope
    outcome: ActionOutcome


_TOOLS = {
    "bind": (JobScope, JobScope),
    "observe": (_EmptyRequest, Observation),
    "execute": (_ExecuteRequest, _ScopedOutcome),
    "verify": (_EmptyRequest, Verification),
}
_REJECTIONS = frozenset({
    "already-bound", "backend-failed", "busy", "duplicate-step", "invalid-request",
    "invalid-permit", "missing-permit", "reconciliation-required", "scope-mismatch",
    "stale-observation", "unauthorized-action", "unbound", "unknown-tool",
})


@asynccontextmanager
async def _exclusive(lock: anyio.Lock) -> AsyncIterator[None]:
    try:
        lock.acquire_nowait()
    except anyio.WouldBlock:
        raise ToolHostError("busy") from None
    try:
        yield
    finally:
        lock.release()


class _ToolHost:
    def __init__(self, backend: ToolBackend) -> None:
        self._backend = backend
        self._scope: JobScope | None = None
        self._observation: Observation | None = None
        self._steps: set[str] = set()
        self._uncertain = False
        self._lock = anyio.Lock()

    async def _observe(self, scope: JobScope) -> Observation:
        observation = await self._backend.observe()
        if not isinstance(observation, Observation):
            raise ToolHostError("backend-failed")
        if observation.scope != scope:
            raise ToolHostError("scope-mismatch")
        return observation

    async def _execute(self, request: _ExecuteRequest, scope: JobScope) -> _ScopedOutcome:
        action, permit = request.action, request.permit
        if action.mutates and permit is None:
            raise ToolHostError("missing-permit")
        if permit is not None:
            if permit.scope != scope:
                raise ToolHostError("scope-mismatch")
            if not action.mutates:
                raise ToolHostError("invalid-permit")
            if permit.step_id in self._steps:
                raise ToolHostError("duplicate-step")
        if self._uncertain:
            raise ToolHostError("reconciliation-required")

        observed = self._observation
        if observed is None or action.observation_id != observed.observation_id:
            raise ToolHostError("stale-observation")
        choice = observed.choice_for(action)
        if choice is None:
            raise ToolHostError("unauthorized-action")
        if permit is not None and (permit.action != action or permit.details != choice.mutation):
            raise ToolHostError("invalid-permit")
        self._observation = None
        if await self._observe(scope) != observed:
            raise ToolHostError("stale-observation")

        if permit is not None:
            # Spend before crossing the UI boundary: cancellation or lost readback
            # does not make the mutation safe to retry in this host.
            self._steps.add(permit.step_id)
            self._uncertain = True
        outcome = await self._backend.execute(action, permit)
        if not isinstance(outcome, ActionOutcome):
            raise ToolHostError("backend-failed")
        self._uncertain = outcome.status == "uncertain"
        return _ScopedOutcome(scope=scope, outcome=outcome)

    async def dispatch(self, request: mcp.CallToolRequestParams) -> BaseModel:
        async with _exclusive(self._lock):
            spec = _TOOLS.get(request.name)
            if spec is None:
                raise ToolHostError("unknown-tool")
            try:
                arguments = spec[0].model_validate(request.arguments or {})
            except ValidationError:
                raise ToolHostError("invalid-request") from None

            if request.name == "bind":
                if self._scope is not None:
                    raise ToolHostError("already-bound")
                assert isinstance(arguments, JobScope)
                await self._observe(arguments)
                self._scope = arguments
                return self._scope
            scope = self._scope
            if scope is None:
                raise ToolHostError("unbound")
            if request.name == "observe":
                self._observation = None
                self._observation = (await self._observe(scope)).model_copy(deep=True)
                return self._observation
            if request.name == "execute":
                assert isinstance(arguments, _ExecuteRequest)
                return await self._execute(arguments, scope)

            self._observation = None
            verified = await self._backend.verify()
            if not isinstance(verified, Verification):
                raise ToolHostError("backend-failed")
            if verified.scope != scope:
                raise ToolHostError("scope-mismatch")
            return verified


async def serve_stdio(backend: ToolBackend) -> None:
    """Serve the supplied backend over one private stdin/stdout connection."""
    host = _ToolHost(backend)
    tools = [
        mcp.Tool(name=name, input_schema=input_type.model_json_schema(), output_schema=output_type.model_json_schema())
        for name, (input_type, output_type) in _TOOLS.items()
    ]

    async def list_tools(_context, _params) -> mcp.ListToolsResult:
        return mcp.ListToolsResult(tools=tools)

    async def call_tool(_context, params: mcp.CallToolRequestParams) -> mcp.CallToolResult:
        try:
            result = await host.dispatch(params)
            return mcp.CallToolResult(content=[], structured_content=result.model_dump(mode="json", by_alias=True))
        except ToolHostError as error:
            code = str(error)
            if code not in _REJECTIONS:
                code = "backend-failed"
        except Exception:
            code = "backend-failed"
        return mcp.CallToolResult(content=[], structured_content={"code": code}, is_error=True)

    server = Server("preparation-tool-host", on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


_Result = TypeVar("_Result", bound=BaseModel)


class ToolHostClient:
    def __init__(self, session: ClientSession, scope: JobScope) -> None:
        self._session = session
        self._scope = scope
        self._lock = anyio.Lock()

    async def _call(self, name: str, request: BaseModel, result_type: type[_Result]) -> _Result:
        async with _exclusive(self._lock):
            try:
                result = await self._session.call_tool(name, request.model_dump(mode="json", by_alias=True))
            except Exception:
                raise ToolHostError("transport-failed") from None
            if not isinstance(result, mcp.CallToolResult):
                raise ToolHostError("invalid-response")
            payload = result.structured_content
            if result.is_error:
                code = payload.get("code") if isinstance(payload, dict) else None
                if isinstance(code, str) and code in _REJECTIONS:
                    raise ToolHostError(code)
                raise ToolHostError("invalid-response")
            if not isinstance(payload, dict):
                raise ToolHostError("invalid-response")
            try:
                return result_type.model_validate(payload)
            except ValidationError:
                raise ToolHostError("invalid-response") from None

    def _check_scope(self, scope: JobScope) -> None:
        if scope != self._scope:
            raise ToolHostError("scope-mismatch")

    async def observe(self) -> Observation:
        result = await self._call("observe", _EmptyRequest(), Observation)
        self._check_scope(result.scope)
        return result

    async def execute(self, action: Action, permit: MutationPermit | None) -> ActionOutcome:
        result = await self._call("execute", _ExecuteRequest(action=action, permit=permit), _ScopedOutcome)
        self._check_scope(result.scope)
        return result.outcome

    async def verify(self) -> Verification:
        result = await self._call("verify", _EmptyRequest(), Verification)
        self._check_scope(result.scope)
        return result


_OS_ENV = frozenset({"APPDATA", "HOME", "LOCALAPPDATA", "SYSTEMROOT", "TEMP", "USERPROFILE"})
_BOOTSTRAP = (
    "import runpy, sys; sys.path.insert(0, sys.argv[1]); sys.argv[:] = sys.argv[2:]; "
    "runpy.run_path(sys.argv[0], run_name='__main__')"
)


@asynccontextmanager
async def open_stdio(
    entry_point: Path, scope: JobScope, *, timeout_seconds: float = 30, assignment_path: Path | None = None,
    display_for_practice: bool = False,
) -> AsyncIterator[ToolHostClient]:
    """Start an application-selected absolute Python script, never a model-selected command."""
    entry_point = Path(entry_point)
    if not entry_point.is_absolute() or entry_point.suffix != ".py" or not entry_point.is_file():
        raise ValueError("The tool host entry point must be a trusted absolute Python script")
    if assignment_path is not None and (not assignment_path.is_absolute() or not assignment_path.is_file()):
        raise ValueError("The tool host assignment must be a protected absolute file")
    root = Path(__file__).resolve().parents[2]
    # MCP v2 merges its default environment into env. Clear the defaults we
    # don't authorize rather than passing the parent environment or an override.
    defaults = get_default_environment()
    env = {key: value if key in _OS_ENV else "" for key, value in defaults.items()}
    if display_for_practice:
        for key in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR"):
            if key in os.environ:
                env[key] = os.environ[key]
    if os.name == "nt":
        env["PATH"] = str(Path(defaults.get("SYSTEMROOT") or r"C:\Windows") / "System32")
    else:
        env["PATH"] = "/usr/bin:/bin"
    args = ["-I", "-B", "-X", "utf8", "-c", _BOOTSTRAP, str(root), str(entry_point.resolve())]
    if assignment_path is not None:
        args.append(str(assignment_path))
    parameters = StdioServerParameters(
        command=sys.executable,
        args=args,
        cwd=root,
        env=env,
    )
    startup_error: ToolHostError | None = None
    operation_error: Exception | None = None
    with open(os.devnull, "w") as discarded_stderr:
        async with stdio_client(parameters, errlog=discarded_stderr) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timeout_seconds) as session:
                try:
                    await session.discover()
                    client = ToolHostClient(session, scope)
                    client._check_scope(await client._call("bind", scope, JobScope))
                except ToolHostError as error:
                    startup_error = error
                except Exception:
                    startup_error = ToolHostError("startup-failed")
                else:
                    try:
                        yield client
                    except Exception as exc:
                        # The SDK's task group must not wrap the caller's domain error.
                        operation_error = exc
    if startup_error is not None:
        raise startup_error
    if operation_error is not None:
        raise operation_error
