"""Authenticated local Sigma evidence companion; native review remains in its own window."""

from __future__ import annotations

import html
import logging
import re
import secrets
import sqlite3
from asyncio import Semaphore
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .preparation.batches import BatchManifest, BatchSlotView, BatchView, ClientAssignment, IntakeClient, NewClient
from .preparation.coordinator_db import CoordinatorDB
from .preparation.local_workspace import LocalWorkspace
from .preparation.review_routes import ReviewBackend, RevisionId, review_router
from .preparation.sigma_config import FirmWorkspace, InspectionWorkspace, IntakeSettings, NativeReviewFactory, SigmaConfig

if TYPE_CHECKING:
    from .preparation.inbox_preparation import InboxPreparation
from .review_store import ReviewError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_CONCURRENT_UPLOADS = 4
CLIENT_ROUTE = re.compile(r"clients(?:/[^/]+/returns/[^/]+)?/?")
log = logging.getLogger(__name__)


class CompanionBackend(ReviewBackend, Protocol):
    async def returns(self) -> list[dict]: ...
    async def jobs(self, return_id: str) -> list[dict] | tuple: ...
    async def intake_settings(self) -> IntakeSettings: ...
    async def batches(self, *, return_id: str | None = None) -> tuple[BatchView, ...]: ...
    async def declare(self, manifest: BatchManifest, *, return_id: str | None = None) -> BatchView: ...
    async def upload(
        self, batch_id: str, slot: str, media_type: str, content: bytes, *, return_id: str | None = None,
    ) -> BatchView: ...
    async def exclude(self, batch_id: str, slot: str) -> BatchView: ...
    async def intake_clients(self) -> tuple[IntakeClient, ...]: ...
    async def create_client(self, client: NewClient) -> IntakeClient: ...
    async def assign_slot(self, batch_id: str, slot: str, assignment: ClientAssignment) -> BatchView: ...


def _committed_batches(batches: tuple[BatchView, ...]) -> tuple[BatchView, ...]:
    if any(slot.state != "uploaded" or not slot.upload_id or not slot.version_id
           for batch in batches for slot in batch.files):
        raise ReviewError(502, "The firm returned an invalid committed upload view.")
    return batches


def _authority(value: str) -> tuple[str, int] | None:
    try:
        parsed = urlsplit("http://" + value)
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return None
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return None
        return parsed.hostname, parsed.port or 80
    except ValueError:
        return None


def create_app(
    data_dir: str | Path = PROJECT_ROOT / "outputs" / "review_workspace",
    ui_dir: str | Path | None = None,
    *,
    port: int = 8765,
    token: str | None = None,
    sigma_config: SigmaConfig | None = None,
    native_review_factory: NativeReviewFactory | None = None,
    local_db: CoordinatorDB | None = None,
    preparation: InboxPreparation | None = None,
) -> FastAPI:
    """Serve the enrolled companion, the local inbox, or a read-only archive."""
    if sigma_config is not None and local_db is not None:
        raise ValueError("Firm enrollment and local inbox storage are mutually exclusive.")
    if preparation is not None and local_db is None:
        raise ValueError("Inbox preparation needs a persistent local inbox.")
    if native_review_factory is not None and sigma_config is None:
        raise ValueError("A trusted native review connector requires an enrolled firm configuration.")
    if sigma_config:
        backend = FirmWorkspace(sigma_config, native_factory=native_review_factory)
    elif local_db is not None:
        backend = LocalWorkspace(Path(data_dir), local_db, preparation=preparation)
    else:
        backend = InspectionWorkspace(Path(data_dir))
    return _companion_app(ui_dir, port=port, token=token, backend=backend)


def _companion_app(
    ui_dir: str | Path | None, *, port: int, token: str | None, backend: CompanionBackend,
) -> FastAPI:
    ui_root = Path(ui_dir or PROJECT_ROOT / "review_ui" / "dist").resolve()
    index_path = ui_root / "index.html"
    if not index_path.is_file():
        raise RuntimeError(
            "Review UI is not built. Run: cd review_ui && "
            "bun install --frozen-lockfile && bun run build"
        )

    index_template = index_path.read_text(encoding="utf-8")
    if "__REVIEW_TOKEN__" not in index_template:
        raise RuntimeError(
            "Built review UI lacks its local token placeholder. Rebuild review_ui."
        )
    upload_slots = Semaphore(MAX_CONCURRENT_UPLOADS)

    secret = token or secrets.token_urlsafe(32)
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if isinstance(backend, FirmWorkspace):
            async with backend:
                yield
        elif isinstance(backend, LocalWorkspace):
            async with backend.processing():
                yield
        else:
            yield

    app = FastAPI(
        title="Review workspace",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.backend = backend
    app.state.review_token = secret

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        def reject(status: int, message: str) -> JSONResponse:
            return JSONResponse(
                {"detail": message}, status_code=status, headers={"Cache-Control": "no-store"}
            )

        hosts = request.headers.getlist("host")
        authority = _authority(hosts[0]) if len(hosts) == 1 else None
        if authority is None or authority[1] != port:
            return reject(403, "Only the configured loopback host is allowed.")
        origins = request.headers.getlist("origin")
        if len(origins) > 1:
            return reject(403, "Multiple origins are not allowed.")
        if origins:
            try:
                origin = urlsplit(origins[0])
                valid_origin = (
                    origin.scheme == "http"
                    and not origin.path
                    and not origin.query
                    and not origin.fragment
                    and _authority(origin.netloc) == authority
                )
            except ValueError:
                valid_origin = False
            if not valid_origin:
                return reject(403, "Cross-origin access is not allowed.")
        if request.headers.get("sec-fetch-site") == "cross-site":
            return reject(403, "Cross-site access is not allowed.")

        is_api = request.url.path == "/api/review" or request.url.path.startswith("/api/review/")
        if is_api:
            supplied = request.headers.getlist("x-review-token")
            authenticated = len(supplied) == 1 and secrets.compare_digest(
                supplied[0].encode(), secret.encode()
            )
            if not authenticated:
                return reject(401, "A valid local review token is required.")

        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "font-src 'self'; connect-src 'self'; worker-src 'self' blob:; "
            "frame-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(ReviewError)
    async def review_error(request: Request, exc: ReviewError):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(sqlite3.Error)
    async def database_error(request: Request, exc: sqlite3.Error):
        code = getattr(exc, "sqlite_errorname", None)
        log.warning("Archived database operation failed (%s)", code or type(exc).__name__)
        detail = "The legacy archive is unavailable. Local inbox storage is separate."
        if code == "SQLITE_READONLY_ROLLBACK":
            detail = "The legacy archive needs SQLite recovery before it can be opened read-only. Keep the database and its journal together."
        return JSONResponse(
            {"detail": detail},
            status_code=503,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        log.error("Unexpected workspace failure (%s)", type(exc).__name__)
        return JSONResponse(
            {"detail": "The local workspace could not complete this request."},
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/review/intake", response_model=IntakeSettings)
    async def intake_settings():
        return await backend.intake_settings()

    @app.get("/api/review/returns/{return_id}/intake/batches", response_model=list[BatchView])
    async def list_return_batches(return_id: RevisionId):
        await backend.access(return_id)
        return _committed_batches(await backend.batches(return_id=return_id))

    @app.post("/api/review/returns/{return_id}/intake/batches", status_code=201, response_model=BatchView)
    async def declare_return_batch(return_id: RevisionId, body: BatchManifest):
        await backend.access(return_id)
        if body.return_hint is not None and body.return_hint != return_id:
            raise ReviewError(409, "The upload hint does not match this return.")
        return await backend.declare(body.model_copy(update={"return_hint": return_id}), return_id=return_id)

    @app.get("/api/review/intake/batches", response_model=list[BatchView])
    async def list_batches():
        return await backend.batches()

    @app.put("/api/review/returns/{return_id}/intake/batches/{batch_id}/files/{slot}", response_model=BatchView)
    async def upload_return_file(return_id: RevisionId, batch_id: RevisionId, slot: RevisionId, request: Request):
        await backend.access(return_id)
        return await receive_upload(batch_id, slot, request, return_id)

    @app.post("/api/review/intake/batches", status_code=201, response_model=BatchView)
    async def declare_batch(body: BatchManifest):
        return await backend.declare(body)

    @app.get("/api/review/intake/clients", response_model=list[IntakeClient])
    async def list_intake_clients():
        return await backend.intake_clients()

    @app.post("/api/review/intake/clients", status_code=201, response_model=IntakeClient)
    async def create_client(body: NewClient):
        return await backend.create_client(body)

    @app.put("/api/review/intake/batches/{batch_id}/files/{slot}/assignment", response_model=BatchView)
    async def assign_slot(batch_id: RevisionId, slot: RevisionId, body: ClientAssignment):
        return await backend.assign_slot(batch_id, slot, body)

    @app.put("/api/review/intake/batches/{batch_id}/files/{slot}", response_model=BatchView)
    async def upload_file(batch_id: RevisionId, slot: RevisionId, request: Request):
        return await receive_upload(batch_id, slot, request)

    async def receive_upload(batch_id: str, slot: str, request: Request, return_id: str | None = None):
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in {"application/pdf", "text/plain"}:
            raise ReviewError(415, "Upload a selectable-text PDF or UTF-8 text file.")
        async with upload_slots:
            content = bytearray()
            async for chunk in request.stream():
                if len(content) + len(chunk) > MAX_UPLOAD_BYTES:
                    raise ReviewError(413, "Documents must be at most 10 MiB.")
                content.extend(chunk)
            if return_id is None:
                return await backend.upload(batch_id, slot, media_type, bytes(content))
            batch = await backend.upload(batch_id, slot, media_type, bytes(content), return_id=return_id)
            return _committed_batches((batch,))[0]

    @app.post("/api/review/intake/batches/{batch_id}/files/{slot}/exclude", response_model=BatchView)
    async def exclude_slot(batch_id: RevisionId, slot: RevisionId):
        return await backend.exclude(batch_id, slot)

    @app.get("/api/review/intake/batches/{batch_id}/files/{slot}/original")
    async def download_inbox_original(batch_id: RevisionId, slot: RevisionId):
        if not isinstance(backend, LocalWorkspace):
            raise ReviewError(404, "Local inbox originals are unavailable in this workspace.")
        media_type, content = await backend.inbox_original(batch_id, slot)
        return Response(content, media_type=media_type, headers={"Content-Disposition": "attachment"})

    @app.get("/api/review/intake/batches/{batch_id}/files/{slot}/evidence", response_model=BatchSlotView)
    async def inbox_evidence(batch_id: RevisionId, slot: RevisionId):
        if not isinstance(backend, LocalWorkspace):
            raise ReviewError(404, "Local inbox evidence is unavailable in this workspace.")
        return await backend.inbox_evidence(batch_id, slot)

    @app.post("/api/review/intake/batches/{batch_id}/files/{slot}/practice", response_model=BatchSlotView)
    async def approve_inbox_practice(batch_id: RevisionId, slot: RevisionId):
        if not isinstance(backend, LocalWorkspace):
            raise ReviewError(404, "Local practice is not enabled in this workspace.")
        return await backend.approve_practice(batch_id, slot)

    @app.get("/api/review/returns")
    async def list_returns():
        return await backend.returns()

    @app.get("/api/review/returns/{return_id}/jobs")
    async def list_jobs(return_id: RevisionId):
        return await backend.jobs(return_id)

    app.include_router(review_router(backend))

    @app.get("/{asset_path:path}")
    def ui(asset_path: str):
        if asset_path in {"", "index.html"} or CLIENT_ROUTE.fullmatch(asset_path):
            response = HTMLResponse(
                index_template.replace("__REVIEW_TOKEN__", html.escape(secret, quote=True))
            )
            return response
        asset = (ui_root / asset_path).resolve()
        if (
            asset_path.startswith("api/")
            or not asset.is_relative_to(ui_root)
            or not asset.is_file()
        ):
            return JSONResponse({"detail": "Not found."}, status_code=404)
        return FileResponse(asset)

    return app
