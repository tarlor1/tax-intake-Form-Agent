"""Return-scoped, one-way native selection push. No values pass through this stream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, StrictInt, ValidationError, model_validator
from starlette.responses import StreamingResponse

from .contracts import Contract
from .tool_contract import Ref

SelectionStatus = Literal["selected", "unmapped", "detached"]
NativeSelection = tuple[str, str, str | None, str | None]


class ReviewUnavailable(LookupError):
    """Expired, closed or not in the caller's authorized scope."""


class ReviewStreamConflict(RuntimeError):
    """The session already has a connected evidence subscriber."""


class _SelectionResponse(StreamingResponse):
    def __init__(self, stream, release):
        super().__init__(stream, media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no",
        })
        self._release = release

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()


class Selection(Contract):
    review_session_id: Ref
    return_id: Ref
    input_revision: Ref
    sequence: Annotated[StrictInt, Field(ge=0)]
    binding_id: Ref | None
    status: SelectionStatus

    @model_validator(mode="after")
    def check_selected_binding(self):
        if (self.status == "selected") != (self.binding_id is not None):
            raise ValueError("Only an identified native selection has a binding ID.")
        return self


@dataclass(frozen=True, slots=True)
class ReviewAccess:
    """Supplied after authenticating the preparer and checking their return assignment."""

    firm_id: str
    preparer_id: str
    return_id: str


@dataclass(frozen=True, slots=True)
class NativeAddress:
    section: str
    record: str | None
    field: str


class ObserverNotice(Contract):
    """Private pipe message. Null draft means no verified native context."""

    sequence: Annotated[StrictInt, Field(ge=1)]
    draft_key: str | None
    section: Ref | None
    record: Ref | None
    field: Ref | None

    @model_validator(mode="after")
    def check_context(self):
        if self.draft_key is None:
            if any(item is not None for item in (self.section, self.record, self.field)):
                raise ValueError("A detached observer cannot identify a native field.")
        elif not self.section or not 0 < len(self.draft_key) <= 500:
            raise ValueError("A native observation must identify its scoped context.")
        return self


class ReviewSelectionSession:
    """An active, already authorized native reservation on the local event loop.

    Access and expiry are checked locally. The launcher closes or revokes this
    stream when it loses the grant; this object cannot release native ownership.
    """

    def __init__(
        self, access: ReviewAccess, *, input_revision: str, draft_key: str, expires_at: datetime,
        bindings: Mapping[NativeAddress, str],
        session_id: str | None = None,
    ) -> None:
        if expires_at.utcoffset() is None or expires_at <= datetime.now(timezone.utc):
            raise ReviewUnavailable("Review grant is not active.")
        if not draft_key:
            raise ValueError("A review session requires a verified native draft key.")
        self.access = access
        self.expires_at = expires_at
        self._draft_key = draft_key
        self._bindings = dict(bindings)
        self._notice_sequence = 0
        self._closed = False
        self._native: NativeSelection | None = None
        self._current = Selection(
            review_session_id=uuid4().hex if session_id is None else session_id,
            return_id=access.return_id, input_revision=input_revision,
            sequence=0, binding_id=None, status="detached",
        )
        self._subscriber: asyncio.Queue[Selection | None] | None = None

    @property
    def id(self) -> str:
        return self._current.review_session_id

    @property
    def input_revision(self) -> str:
        return self._current.input_revision

    @property
    def active(self) -> bool:
        return not self._closed and datetime.now(timezone.utc) < self.expires_at

    def require_access(self, access: ReviewAccess, session_id: str) -> None:
        if access != self.access or session_id != self.id or not self.active:
            raise ReviewUnavailable("Review session is not available in this scope.")

    def native_selection(self, notice: NativeSelection | None) -> None:
        """Called on the local event loop by the dedicated observer bridge."""
        if not self.active:
            return
        if notice is not None and notice[0] != self._draft_key:
            notice = None
        if notice == self._native:
            return
        self._native = notice
        binding = None
        status: SelectionStatus = "detached"
        if notice is not None:
            _, section, record, field = notice
            binding = self._bindings.get(NativeAddress(section, record, field)) if field else None
            status = "selected" if binding else "unmapped"
        self._current = Selection(
            review_session_id=self.id, return_id=self.access.return_id, input_revision=self.input_revision,
            sequence=self._current.sequence + 1, binding_id=binding, status=status,
        )
        if self._subscriber is None:
            return
        try:
            self._subscriber.put_nowait(self._current)
        except asyncio.QueueFull:
            # A stalled stream becomes disconnected; reconnect receives a single
            # current snapshot, never an invented sequence of missed stops.
            self._disconnect()

    def _disconnect(self) -> None:
        outgoing, self._subscriber = self._subscriber, None
        if outgoing is not None:
            while not outgoing.empty():
                outgoing.get_nowait()
            outgoing.put_nowait(None)

    def close(self) -> None:
        """Stop local delivery. Only the native authority can release ownership."""
        self._closed = True
        self._disconnect()

    def _claim(self, access: ReviewAccess, session_id: str) -> asyncio.Queue[Selection | None]:
        self.require_access(access, session_id)
        if self._subscriber is not None:
            raise ReviewStreamConflict("Evidence is already following this review in another window.")
        queue: asyncio.Queue[Selection | None] = asyncio.Queue(maxsize=1024)
        # Claim and snapshot are atomic on the local event loop.
        queue.put_nowait(self._current)
        self._subscriber = queue
        return queue

    def _release(self, queue: asyncio.Queue[Selection | None]) -> None:
        if self._subscriber is queue:
            self._disconnect()

    async def _selections(self, queue: asyncio.Queue[Selection | None]) -> AsyncIterator[Selection]:
        while self.active:
            try:
                remaining = (self.expires_at - datetime.now(timezone.utc)).total_seconds()
                async with asyncio.timeout(max(0, remaining)):
                    selection = await queue.get()
            except TimeoutError:
                return
            if selection is None or not self.active:
                return
            yield selection

    @asynccontextmanager
    async def subscribe(self, access: ReviewAccess, session_id: str) -> AsyncIterator[AsyncIterator[Selection]]:
        queue = self._claim(access, session_id)
        try:
            yield self._selections(queue)
        finally:
            self._release(queue)

    async def relay(self, pipe: asyncio.StreamReader) -> None:
        """Read opaque line-delimited selection notices from a private bound child pipe."""
        try:
            while self.active:
                raw = await pipe.readline()
                if not raw or len(raw) > 2048:
                    break
                try:
                    notice = ObserverNotice.model_validate_json(raw)
                except ValidationError:
                    break
                # A closed child may have buffered some older notifications.
                if notice.sequence <= self._notice_sequence:
                    continue
                if notice.sequence != self._notice_sequence + 1:
                    break
                self._notice_sequence = notice.sequence
                self.native_selection(
                    (notice.draft_key, notice.section, notice.record, notice.field)
                    if notice.draft_key is not None and notice.section is not None else None
                )
        finally:
            self.native_selection(None)

    def event_response(self, access: ReviewAccess, session_id: str) -> StreamingResponse:
        queue = self._claim(access, session_id)

        async def stream() -> AsyncIterator[str]:
            try:
                async for selection in self._selections(queue):
                    yield f"event: selection\ndata: {selection.model_dump_json(by_alias=True)}\n\n"
            finally:
                self._release(queue)

        return _SelectionResponse(stream(), lambda: self._release(queue))
