"""Manually exercise real uploads locally with synthetic files only.

Run from the repository root: PYTHONPATH=. python tests/practice_inbox.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import socket
from tempfile import TemporaryDirectory

import uvicorn

from sigma_practice_uploads import PracticeObjects
from src.preparation.batches import BatchIntake, BatchManifest, BatchView, IngestScope
from src.preparation.dispatch import ExtractionPolicy
from src.preparation.sigma_config import InspectionWorkspace, IntakeSettings
from src.review_api import _companion_app
from src.review_store import ReviewError
from src.local_postgres import local_postgres


class PracticeInbox(InspectionWorkspace):
    """Use the reference upload writer, without a worker or a native target."""

    def __init__(
        self, directory: Path, intake: BatchIntake, scope: IngestScope, *, policy: ExtractionPolicy | None = None,
    ) -> None:
        super().__init__(directory)
        self._intake = intake
        self._scope = scope
        self._policy = policy

    async def intake_settings(self) -> IntakeSettings:
        return IntakeSettings(
            enabled=True,
            approval_label="Approve synthetic files for practice extraction." if self._policy else None,
            message="Practice only. Use synthetic files. No documents leave this machine.",
        )

    async def batches(self, *, return_id: str | None = None) -> tuple[BatchView, ...]:
        return await asyncio.to_thread(self._intake.list_batches, self._scope, return_id=return_id)

    async def declare(self, manifest: BatchManifest, *, return_id: str | None = None) -> BatchView:
        if return_id is not None:
            if return_id not in self._scope.return_ids:
                raise ReviewError(404, "Return not found in the upload scope.")
            if manifest.return_hint not in (None, return_id):
                raise ReviewError(409, "The upload hint does not match this return.")
            manifest = manifest.model_copy(update={"return_hint": return_id})
        if manifest.approved_for_ai and self._policy is None:
            raise ReviewError(409, "External processing is disabled in this practice inbox.")
        return await asyncio.to_thread(self._intake.declare, self._scope, manifest, policy=self._policy)

    async def upload(
        self, batch_id: str, slot: str, media_type: str, content: bytes, *, return_id: str | None = None,
    ) -> BatchView:
        return await asyncio.to_thread(
            self._intake.put, self._scope, batch_id, slot, media_type, content, return_id=return_id,
        )

    async def exclude(self, batch_id: str, slot: str, *, return_id: str | None = None) -> BatchView:
        return await asyncio.to_thread(self._intake.exclude, self._scope, batch_id, slot, return_id=return_id)


def main() -> None:
    with TemporaryDirectory(prefix="sigma-inbox-") as temporary:
        directory = Path(temporary).resolve()
        with local_postgres(directory) as db, socket.socket() as listener:
            intake = BatchIntake(db, PracticeObjects(directory))
            intake.install()
            backend = PracticeInbox(directory, intake, IngestScope("practice", "local-reviewer", frozenset()))
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            app = _companion_app(None, port=port, token=None, backend=backend)
            server = uvicorn.Server(uvicorn.Config(
                app, host="127.0.0.1", port=port, proxy_headers=False, access_log=False,
            ))
            print(f"Practice inbox: http://127.0.0.1:{port}/clients (temporary uploads, Ctrl+C to stop)", flush=True)
            server.run(sockets=[listener])


if __name__ == "__main__":
    main()
