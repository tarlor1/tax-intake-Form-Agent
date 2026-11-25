"""Loopback workspace intake with durable originals and no external disclosure."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path

from psycopg import Error as PostgresError

from ..review_store import ReviewError
from .batches import (
    BatchIntake, BatchManifest, BatchSlotView, BatchView, ClientAssignment, IngestScope,
    IntakeClient, MediaType, NewClient,
)
from .coordinator_db import CoordinatorDB
from .inbox_preparation import InboxPreparation
from .local_analysis import analyze_document
from .local_document_index import LocalDocumentIndex
from .originals import FilesystemOriginals
from .sigma_config import InspectionWorkspace, IntakeSettings

_SCOPE = IngestScope("local-workspace", "local-operator", frozenset())
log = logging.getLogger(__name__)


class LocalWorkspace(InspectionWorkspace):
    """Store client documents locally without opening archived returns."""

    def __init__(self, data_dir: Path, db: CoordinatorDB, *, preparation: InboxPreparation | None = None):
        super().__init__(None)
        originals = data_dir.expanduser().absolute() / "inbox-originals"
        originals.mkdir(mode=0o700, parents=True, exist_ok=True)
        objects = FilesystemOriginals(originals)
        self._intake = BatchIntake(db, objects, require_assignment=True)
        self._intake.install()
        self._preparation = preparation
        self._index = LocalDocumentIndex(db, _SCOPE, objects, analyze_document)
        self._index.install()
        self._work_available = asyncio.Event()
        self._preparation_available = asyncio.Event()

    @asynccontextmanager
    async def processing(self):
        stopping = False

        async def extract_one() -> bool:
            advanced = await asyncio.to_thread(self._index.process_one)
            if advanced:
                self._preparation_available.set()
            return advanced

        async def prepare_one() -> bool:
            assert self._preparation is not None
            scheduled = await asyncio.to_thread(self._preparation.dispatch_one, _SCOPE)
            processed = await self._preparation.run_once()
            return scheduled or processed is not None

        async def drain(available, advance) -> None:
            while not stopping:
                available.clear()
                try:
                    while not stopping and await advance():
                        pass
                except PostgresError as error:
                    log.warning("Local document queue unavailable (%s)", type(error).__name__)
                    if not stopping:
                        try:
                            await asyncio.wait_for(available.wait(), timeout=2)
                        except TimeoutError:
                            pass
                else:
                    if not stopping:
                        await available.wait()

        tasks = [asyncio.create_task(drain(self._work_available, extract_one))]
        if self._preparation is not None:
            tasks.append(asyncio.create_task(drain(self._preparation_available, prepare_one)))
        try:
            yield
        finally:
            stopping = True
            self._work_available.set()
            self._preparation_available.set()
            await asyncio.gather(*tasks)

    def _annotate(self, batches: tuple[BatchView, ...]) -> tuple[BatchView, ...]:
        analyzed = self._index.annotate(batches)
        return self._preparation.annotate(_SCOPE, analyzed) if self._preparation is not None else analyzed

    def _list_local_batches(self) -> tuple[BatchView, ...]:
        return self._annotate(self._intake.list_batches(_SCOPE))

    def _annotate_batch(self, batch: BatchView) -> BatchView:
        annotated, = self._annotate((batch,))
        return annotated

    def _store_upload(
        self, batch_id: str, slot: str, media_type: str, content: bytes, loop: asyncio.AbstractEventLoop,
    ) -> BatchView:
        batch = self._intake.put(_SCOPE, batch_id, slot, media_type, content)
        # Wake after the commit even if the HTTP request was cancelled while the
        # storage thread finished writing.
        loop.call_soon_threadsafe(self._work_available.set)
        return self._annotate_batch(batch)

    async def intake_settings(self) -> IntakeSettings:
        return IntakeSettings(
            enabled=True,
            can_download_originals=True,
            requires_client=True,
            can_create_clients=True,
            approval_label=self._preparation.approval_label if self._preparation is not None else None,
            message=self._preparation.description if self._preparation is not None else
            "Documents are extracted on this computer.",
        )

    async def batches(self, *, return_id: str | None = None) -> tuple[BatchView, ...]:
        if return_id is not None:
            return ()  # Local uploads cannot be attributed to archived returns.
        return await asyncio.to_thread(self._list_local_batches)

    async def declare(self, manifest: BatchManifest, *, return_id: str | None = None) -> BatchView:
        if return_id is not None or manifest.return_hint is not None:
            raise ReviewError(409, "Local uploads cannot be attached to archived returns.")
        if manifest.assignment is not None:
            await asyncio.to_thread(self._intake.require_local_client, manifest.assignment)
        if manifest.approved_for_ai and self._preparation is None:
            raise ReviewError(409, "No preparation service is configured.")
        recipient = self._preparation.recipient if self._preparation is not None else None
        batch = await asyncio.to_thread(self._intake.declare, _SCOPE, manifest, policy=recipient)
        return await asyncio.to_thread(self._annotate_batch, batch)

    async def intake_clients(self) -> tuple[IntakeClient, ...]:
        return await asyncio.to_thread(self._intake.local_clients)

    async def create_client(self, client: NewClient) -> IntakeClient:
        return await asyncio.to_thread(self._intake.create_local_client, client)

    async def assign_slot(self, batch_id: str, slot: str, assignment: ClientAssignment) -> BatchView:
        await asyncio.to_thread(self._intake.require_local_client, assignment)
        batch = await asyncio.to_thread(self._intake.assign_slot, _SCOPE, batch_id, slot, assignment)
        self._preparation_available.set()
        return await asyncio.to_thread(self._annotate_batch, batch)

    async def upload(
        self, batch_id: str, slot: str, media_type: str, content: bytes, *, return_id: str | None = None,
    ) -> BatchView:
        if return_id is not None:
            raise ReviewError(409, "Use the general inbox to store local originals.")
        return await asyncio.to_thread(self._store_upload, batch_id, slot, media_type, content, asyncio.get_running_loop())

    async def exclude(self, batch_id: str, slot: str) -> BatchView:
        batch = await asyncio.to_thread(self._intake.exclude, _SCOPE, batch_id, slot)
        return await asyncio.to_thread(self._annotate_batch, batch)

    async def inbox_original(self, batch_id: str, slot: str) -> tuple[MediaType, bytes]:
        return await asyncio.to_thread(self._intake.original, _SCOPE, batch_id, slot)

    def _read_evidence(self, batch_id: str, slot_id: str) -> BatchSlotView:
        batch = self._annotate_batch(self._intake.status(_SCOPE, batch_id))
        for slot in batch.files:
            if slot.slot == slot_id and slot.state == "uploaded":
                return slot
        raise ReviewError(404, "No original was committed for this slot.")

    async def inbox_evidence(self, batch_id: str, slot: str) -> BatchSlotView:
        return await asyncio.to_thread(self._read_evidence, batch_id, slot)

    async def approve_practice(self, batch_id: str, slot: str) -> BatchSlotView:
        if self._preparation is None:
            raise ReviewError(409, "No preparation service is configured.")
        await asyncio.to_thread(self._preparation.approve, _SCOPE, batch_id, slot)
        self._preparation_available.set()
        return await self.inbox_evidence(batch_id, slot)
