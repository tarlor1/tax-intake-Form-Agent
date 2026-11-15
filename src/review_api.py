"""Authenticated local document intake, unified review, and browser execution.

Gemini extraction requires local configuration and explicit approved-data consent.
The unchanged intake website is a separate origin, controlled only through its UI.
"""

from __future__ import annotations

import html
import logging
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Callable, Literal
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr
from starlette.concurrency import run_in_threadpool

from .review_jobs import ReviewJobs
from .review_intake import ReviewIntake
from .review_store import ReviewError, ReviewStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
CLIENT_ROUTE = re.compile(r"clients(?:/[^/]+/returns/[^/]+)?/?")
log = logging.getLogger(__name__)


class RevisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    expectedRevision: StrictInt = Field(ge=1)


class SelectBody(RevisionBody):
    fieldId: StrictStr = Field(min_length=1, max_length=150)


class ResolveBody(SelectBody):
    candidateId: StrictStr = Field(min_length=1, max_length=250)


class CorrectBody(SelectBody):
    value: StrictStr = Field(max_length=10000)
    citationIds: list[StrictStr] = Field(default_factory=list, max_length=50)
    reason: StrictStr = Field(default="", max_length=2000)


class ProposalBody(SelectBody):
    documentId: StrictStr = Field(min_length=1, max_length=250)
    page: StrictInt = Field(ge=1)
    rect: list[StrictFloat] = Field(min_length=4, max_length=4)
    value: StrictStr = Field(min_length=1, max_length=500)
    reason: StrictStr = Field(default="", max_length=2000)


class IntakeProcessBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    documentIds: list[Annotated[StrictStr, Field(min_length=1, max_length=150)]] = Field(
        min_length=1, max_length=100
    )
    approvedForAI: StrictBool = False


class ExtractBody(RevisionBody):
    approvedForAI: StrictBool = False


class AssignmentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groupId: StrictStr = Field(min_length=1, max_length=150)
    returnId: Annotated[StrictStr, Field(min_length=1, max_length=150)] | None = None
    clientName: Annotated[StrictStr, Field(min_length=1, max_length=200)] | None = None
    taxYear: Annotated[StrictInt, Field(ge=1900, le=2200)] | None = None
    returnType: Literal["individual", "business"] | None = None


class BrowserAction(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    reason: StrictStr = Field(default="", max_length=2000)


class BrowserClick(BrowserAction):
    kind: Literal["click"]
    x: StrictFloat = Field(ge=0, le=4096)
    y: StrictFloat = Field(ge=0, le=4096)


class BrowserWheel(BrowserAction):
    kind: Literal["wheel"]
    x: StrictFloat = Field(ge=0, le=4096)
    y: StrictFloat = Field(ge=0, le=4096)
    deltaY: StrictFloat = Field(ge=-2000, le=2000)


class BrowserKey(BrowserAction):
    kind: Literal["key"]
    key: StrictStr = Field(min_length=1, max_length=60)


class BrowserText(BrowserAction):
    kind: Literal["text"]
    text: StrictStr = Field(min_length=1, max_length=10000)


class BrowserFocus(BrowserAction):
    kind: Literal["focus"]
    fieldId: StrictStr = Field(min_length=1, max_length=150, pattern=r"^[a-z][a-z0-9_]*$")


BrowserInput = Annotated[
    BrowserClick | BrowserWheel | BrowserKey | BrowserText | BrowserFocus,
    Field(discriminator="kind"),
]


class BrowserSource(ResolveBody):
    reason: StrictStr = Field(default="", max_length=2000)


class BrowserField(SelectBody):
    value: StrictStr = Field(max_length=10000)
    reason: StrictStr = Field(default="", max_length=2000)


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
    max_workers: int = 2,
    headless: bool = True,
    target_url: str = "http://127.0.0.1:8766/index.html",
) -> FastAPI:
    """Serve the companion; the target website is independently hosted and unchanged."""
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

    secret = token or secrets.token_urlsafe(32)
    store = ReviewStore(data_dir)
    jobs = ReviewJobs(
        store,
        target_url=target_url,
        max_workers=max_workers,
        headless=headless,
    )

    def prepare_return(return_id: str) -> None:
        # Read and enqueue under the supervisor's mutation lock so the snapshot
        # uses the latest committed extraction revision, not an earlier group.
        with jobs.lock:
            detail = store.get_return(return_id)
            jobs.enqueue(return_id, detail["revision"])

    try:
        intake = ReviewIntake(
            store,
            mutation_lock=jobs.lock,
            ensure_idle=jobs.ensure_idle,
            prepare_return=prepare_return,
        )
    except BaseException:
        jobs.close()
        raise

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        jobs.start()
        try:
            yield
        finally:
            try:
                await run_in_threadpool(intake.close)
            finally:
                await run_in_threadpool(jobs.close)

    app = FastAPI(
        title="Review workspace",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.jobs = jobs
    app.state.intake = intake
    app.state.review_token = secret
    app.state.target_url = jobs.target_url

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
        log.exception("Review database operation failed")
        return JSONResponse(
            {"detail": "The local database is unavailable. No successful save is being reported."},
            status_code=503,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        log.error("Unexpected workspace failure", exc_info=(type(exc), exc, exc.__traceback__))
        return JSONResponse(
            {"detail": "The local workspace could not complete this request."},
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )

    def edit_return(return_id: str, change: Callable[[], dict]) -> dict:
        # Scheduling and review edits share one lock, so a worker cannot claim a
        # return between its ownership check and the committed review mutation.
        with jobs.lock:
            jobs.ensure_idle(return_id)
            return change()

    async def read_upload(request: Request, encoded_name: str) -> tuple[str, bytes, str]:
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in {"application/pdf", "text/plain"}:
            raise ReviewError(415, "Upload a selectable-text PDF or UTF-8 text file.")
        try:
            filename = unquote(encoded_name, errors="strict")
        except UnicodeError as exc:
            raise ReviewError(422, "The document filename is not valid UTF-8.") from exc
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_UPLOAD_BYTES:
                raise ReviewError(413, "Documents must be at most 10 MiB.")
        return filename, bytes(content), media_type

    @app.get("/api/review/intake")
    def get_intake():
        return {**intake.config(), "documents": intake.list_documents()}

    @app.get("/api/review/target")
    def get_target():
        return {"url": jobs.target_url, "target": "intake_form/index.html"}

    @app.post("/api/review/intake/documents", status_code=201)
    async def upload_intake_document(
        request: Request,
        document_name: Annotated[str, Header(alias="X-Document-Name", max_length=1000)],
    ):
        filename, content, media_type = await read_upload(request, document_name)
        return await run_in_threadpool(intake.upload, filename, content, media_type)

    @app.post("/api/review/intake/process")
    def process_intake(body: IntakeProcessBody):
        return intake.process(body.documentIds, body.approvedForAI)

    @app.post("/api/review/intake/documents/{document_id}/assign")
    def assign_intake(document_id: str, body: AssignmentBody):
        return intake.assign(
            document_id,
            body.groupId,
            body.returnId,
            body.clientName,
            body.taxYear,
            body.returnType,
        )

    @app.get("/api/review/returns")
    def list_returns():
        return store.list_returns()

    @app.get("/api/review/returns/{return_id}")
    def get_return(return_id: str):
        return store.get_return(return_id)

    @app.get("/api/review/returns/{return_id}/documents/{document_id}/pdf")
    def get_pdf(return_id: str, document_id: str):
        return Response(
            store.get_pdf(return_id, document_id),
            media_type="application/pdf",
            headers={"Content-Disposition": 'inline; filename="review-source.pdf"'},
        )

    @app.get("/api/review/returns/{return_id}/documents/{document_id}/text")
    def get_text(return_id: str, document_id: str):
        return Response(
            store.get_text(return_id, document_id),
            media_type="text/plain",
            headers={"Content-Disposition": 'inline; filename="review-source.txt"'},
        )

    @app.post("/api/review/returns/{return_id}/select")
    def select_field(return_id: str, body: SelectBody):
        return edit_return(
            return_id, lambda: store.select(return_id, body.expectedRevision, body.fieldId)
        )

    @app.post("/api/review/returns/{return_id}/resolve")
    def resolve(return_id: str, body: ResolveBody):
        return edit_return(
            return_id,
            lambda: store.resolve(return_id, body.expectedRevision, body.fieldId, body.candidateId),
        )

    @app.post("/api/review/returns/{return_id}/correct")
    def correct(return_id: str, body: CorrectBody):
        return edit_return(
            return_id,
            lambda: store.correct(
                return_id,
                body.expectedRevision,
                body.fieldId,
                body.value,
                body.citationIds,
                body.reason,
            ),
        )

    @app.post("/api/review/returns/{return_id}/documents")
    async def upload_document(
        return_id: str,
        request: Request,
        expected_revision: Annotated[int, Header(alias="X-Expected-Revision", ge=1)],
        document_name: Annotated[str, Header(alias="X-Document-Name", max_length=1000)],
    ):
        filename, content, media_type = await read_upload(request, document_name)
        return await run_in_threadpool(
            edit_return,
            return_id,
            lambda: store.add_document(
                return_id, expected_revision, filename, content, media_type=media_type
            ),
        )

    @app.post("/api/review/returns/{return_id}/extract")
    def extract_return(return_id: str, body: ExtractBody):
        return intake.process_return(return_id, body.expectedRevision, body.approvedForAI)

    @app.post("/api/review/returns/{return_id}/proposals")
    def add_proposal(return_id: str, body: ProposalBody):
        return edit_return(
            return_id,
            lambda: store.add_proposal(
                return_id,
                body.expectedRevision,
                body.fieldId,
                body.documentId,
                body.page,
                body.rect,
                body.value,
                body.reason,
            ),
        )

    @app.get("/api/review/returns/{return_id}/jobs")
    def list_jobs(return_id: str):
        return jobs.list_jobs(return_id)

    @app.post("/api/review/returns/{return_id}/jobs")
    def enqueue_job(return_id: str, body: RevisionBody):
        return jobs.enqueue(return_id, body.expectedRevision)

    @app.post("/api/review/returns/{return_id}/jobs/{job_id}/cancel")
    def cancel_job(return_id: str, job_id: str):
        return jobs.cancel(return_id, job_id)

    @app.get("/api/review/returns/{return_id}/jobs/{job_id}/frame")
    def browser_frame(return_id: str, job_id: str):
        frame = jobs.browser_frame(return_id, job_id)
        if frame is None:
            return Response(status_code=204)
        return Response(frame, media_type="image/jpeg")

    @app.get("/api/review/returns/{return_id}/browser")
    def browser_state(return_id: str):
        return jobs.browser_state(return_id)

    @app.post("/api/review/returns/{return_id}/jobs/{job_id}/interact", status_code=202)
    def interact_browser(return_id: str, job_id: str, body: BrowserInput):
        return jobs.interact(return_id, job_id, body.model_dump())

    @app.post("/api/review/returns/{return_id}/jobs/{job_id}/source", status_code=202)
    def choose_browser_source(return_id: str, job_id: str, body: BrowserSource):
        return jobs.choose_source(
            return_id,
            job_id,
            body.expectedRevision,
            body.fieldId,
            body.candidateId,
            reason=body.reason,
        )

    @app.post("/api/review/returns/{return_id}/jobs/{job_id}/field", status_code=202)
    def apply_browser_field(return_id: str, job_id: str, body: BrowserField):
        return jobs.apply_field(
            return_id,
            job_id,
            body.expectedRevision,
            body.fieldId,
            body.value,
            reason=body.reason,
        )

    @app.post("/api/review/returns/{return_id}/jobs/{job_id}/submit", status_code=202)
    def submit_browser_form(return_id: str, job_id: str, body: RevisionBody):
        return jobs.submit_browser(return_id, job_id, body.expectedRevision)

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
