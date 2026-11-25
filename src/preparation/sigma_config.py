"""Deployment configuration for the companion; no credential or fixture fallback."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from decimal import Decimal
import os
from pathlib import Path
import re
from typing import Annotated, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import Field, ValidationError, field_validator

from ..review_store import ReviewError
from .agent import DataClass, Stage, StagePolicy
from .batches import BatchManifest, BatchView, ClientAssignment, IntakeClient, MediaType, NewClient
from .contracts import Contract
from .legacy_inspection import LegacyInspection
from .providers import ProviderIdentity
from .review_events import ReviewAccess, ReviewSelectionSession, ReviewUnavailable
from .review_routes import EvidenceDetail, ReviewDecision, ReviewDecisionRecorded, ReviewOpened
from .tool_contract import Ref


class ModelEnrollment(Contract):
    provider: Literal["gemini", "openai", "anthropic"]
    model: Ref
    endpoint: str
    account: Ref
    classes: frozenset[DataClass]
    max_requests: Annotated[int, Field(gt=0, le=1000)]
    max_request_bytes: Annotated[int, Field(gt=0, le=2_000_000)]
    max_seconds: Annotated[int, Field(gt=0, le=3600)]
    max_output_tokens: Annotated[int, Field(gt=0, le=65_536)]
    max_billable_input_tokens: Annotated[int, Field(gt=0)]
    max_billable_output_tokens: Annotated[int, Field(gt=0)]
    input_price_per_million: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    output_price_per_million: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    max_spend: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]

    def stage_policy(self, stage: Stage) -> StagePolicy:
        return StagePolicy(
            identity=ProviderIdentity(self.provider, self.model, self.endpoint, self.account),
            stage=stage,
            document_classes=self.classes,
            max_requests=self.max_requests,
            max_request_bytes=self.max_request_bytes,
            timeout_seconds=self.max_seconds,
            max_input_tokens=self.max_billable_input_tokens,
            max_output_tokens=self.max_billable_output_tokens,
            input_price_per_million=self.input_price_per_million,
            output_price_per_million=self.output_price_per_million,
            max_spend=self.max_spend,
            request_output_tokens=self.max_output_tokens,
        )

    @field_validator("endpoint")
    @classmethod
    def check_endpoint(cls, value: str) -> str:
        return _https(value, allow_path=True)


def _https(value: str, *, allow_path: bool) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or value != value.rstrip("/") or (parsed.path and not allow_path)):
        raise ValueError("Use a canonical HTTPS endpoint without credentials, query, or fragment.")
    return value


class SigmaConfig(Contract):
    schema_version: Literal[1]
    firm_id: Ref
    machine_id: Ref
    service_origin: str
    product: Literal["web", "desktop"]
    tax_year: Annotated[int, Field(ge=1900, le=2200)]
    profile_id: Ref
    extraction: ModelEnrollment | None = None
    reasoning: ModelEnrollment | None = None
    allow_images: Literal[False] = False

    @field_validator("service_origin")
    @classmethod
    def check_origin(cls, value: str) -> str:
        return _https(value, allow_path=False)

    @classmethod
    def read(cls, path: Path) -> SigmaConfig:
        try:
            return cls.model_validate_json(path.expanduser().read_bytes())
        except (OSError, ValidationError):
            raise ValueError("Sigma enrollment file is unreadable or does not match schema version 1.") from None


class IntakeSettings(Contract):
    enabled: bool
    approval_label: str | None = None
    message: str | None = None
    can_download_originals: bool = False
    requires_client: bool = False
    can_create_clients: bool = False


class NativeReview(Protocol):
    async def open(self, access: ReviewAccess, revision: str) -> ReviewOpened: ...
    def session(self, access: ReviewAccess, session_id: str) -> ReviewSelectionSession: ...
    async def close(self, access: ReviewAccess, session_id: str) -> None: ...


class FirmJSON(Protocol):
    def __call__(self, method: str, path: str, **parameters) -> Awaitable: ...


NativeReviewFactory = Callable[[SigmaConfig, FirmJSON], NativeReview]


class InspectionWorkspace(LegacyInspection):
    """Local read-only mode. The archived SQLite writer is never constructed."""

    async def intake_settings(self) -> IntakeSettings:
        return IntakeSettings(enabled=False, message="Connect the firm’s practice service to accept uploads for preparation.")

    async def batches(self, *, return_id: str | None = None) -> tuple[BatchView, ...]:
        return ()

    async def declare(self, manifest: BatchManifest, *, return_id: str | None = None) -> BatchView:
        raise ReviewError(409, "Uploads require an enrolled firm service.")

    async def upload(
        self, batch_id: str, slot: str, media_type: str, content: bytes, *, return_id: str | None = None,
    ) -> BatchView:
        raise ReviewError(409, "Uploads require an enrolled firm service.")

    async def exclude(self, batch_id: str, slot: str) -> BatchView:
        raise ReviewError(409, "Uploads require an enrolled firm service.")

    async def intake_clients(self) -> tuple[IntakeClient, ...]:
        return ()

    async def create_client(self, client: NewClient) -> IntakeClient:
        raise ReviewError(409, "Client creation is unavailable in this workspace.")

    async def assign_slot(self, batch_id: str, slot: str, assignment: ClientAssignment) -> BatchView:
        raise ReviewError(409, "Document assignment is unavailable in this workspace.")


@dataclass(frozen=True, slots=True)
class _AccessReply:
    firm_id: str
    preparer_id: str
    return_id: str


class FirmWorkspace:
    """Local client of a provisioned firm service, authenticated as one preparer.

    The launcher inherits a short-lived individual access token provisioned
    by the firm's SSO integration. The firm validates it and the return grant
    on each request. Browser pages and the native subprocess never receive it.
    A matching local native review integration must be enrolled separately.
    """

    def __init__(self, config: SigmaConfig, *, native_factory: NativeReviewFactory | None = None):
        self._config = config
        self._http: httpx.AsyncClient | None = None
        self._native = native_factory(config, self._json) if native_factory is not None else None

    async def __aenter__(self) -> FirmWorkspace:
        token = os.environ.get("SIGMA_REVIEW_ACCESS_TOKEN")
        if not token or not re.fullmatch(r"[A-Za-z0-9_.~+/=-]{16,8192}", token):
            raise RuntimeError("An individual firm SSO access token is required for this enrollment.")
        self._http = httpx.AsyncClient(
            base_url=self._config.service_origin, trust_env=False, follow_redirects=False, timeout=15,
            headers={"Authorization": f"Bearer {token}", "X-Sigma-Profile": self._config.profile_id,
                     "X-Sigma-Machine": self._config.machine_id},
        )
        return self

    async def __aexit__(self, *_unused) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self._http is None:
            raise ReviewError(503, "The firm service is not connected.")
        try:
            response = await self._http.request(method, f"/api/sigma/v1{path}", **kwargs)
        except httpx.HTTPError:
            raise ReviewError(503, "Unable to reach the approved firm service.") from None
        if response.status_code in (401, 403):
            raise ReviewError(403, "The firm denied the request. Sign in again or check the return assignment.")
        if response.status_code in (404, 409, 413, 415, 422):
            raise ReviewError(response.status_code, "The firm could not apply the request to the assigned return or file slot.")
        if not response.is_success:
            raise ReviewError(503, "The approved firm service could not complete the request.")
        return response

    async def _json(self, method: str, path: str, **kwargs):
        response = await self._request(method, path, **kwargs)
        try:
            return response.json()
        except ValueError:
            raise ReviewError(502, "The firm service returned an invalid response.") from None

    async def returns(self) -> list[dict]:
        return await self._json("GET", "/returns")

    async def jobs(self, return_id: str) -> list[dict]:
        await self.access(return_id)
        return await self._json("GET", f"/returns/{return_id}/jobs")

    async def access(self, return_id: str) -> ReviewAccess:
        reply = await self._json("GET", f"/returns/{return_id}/access")
        try:
            access = _AccessReply(reply["firmId"], reply["preparerId"], reply["returnId"])
        except (TypeError, KeyError):
            raise ReviewError(502, "The firm service returned an invalid grant.") from None
        if access.firm_id != self._config.firm_id or access.return_id != return_id or not access.preparer_id:
            raise ReviewError(404, "Return not found in this enrollment.")
        return ReviewAccess(access.firm_id, access.preparer_id, return_id)

    async def evidence(self, access: ReviewAccess, revision: str | None) -> EvidenceDetail:
        path = f"/returns/{access.return_id}/evidence"
        payload = await self._json("GET", path, params={"revision": revision} if revision else {})
        detail = EvidenceDetail.model_validate(payload)
        if self._native is None:
            return detail.model_copy(update={"review_available": False,
                "review_message": "No licensed native review connector is enrolled on this machine."})
        return detail

    async def original(self, access: ReviewAccess, revision: str, document_id: str, version_id: str) -> tuple[MediaType, bytes]:
        response = await self._request("GET", f"/returns/{access.return_id}/originals/{document_id}/{version_id}",
                                       params={"revision": revision})
        media_type = response.headers.get("content-type", "").split(";")[0]
        if media_type not in {"application/pdf", "text/plain"} or len(response.content) > 10 * 1024 * 1024:
            raise ReviewError(502, "The service returned an invalid original.")
        return media_type, response.content

    async def intake_settings(self) -> IntakeSettings:
        settings = IntakeSettings.model_validate(await self._json("GET", "/intake"))
        if self._config.extraction is None:
            return settings.model_copy(update={"approval_label": None})
        return settings

    async def batches(self, return_id: str | None = None) -> tuple[BatchView, ...]:
        path = f"/returns/{return_id}/intake/batches" if return_id else "/intake/batches"
        return tuple(BatchView.model_validate(row) for row in await self._json("GET", path))

    async def declare(self, manifest: BatchManifest, return_id: str | None = None) -> BatchView:
        if manifest.approved_for_ai and self._config.extraction is None:
            raise ReviewError(409, "No approved external processing policy is enrolled.")
        path = f"/returns/{return_id}/intake/batches" if return_id else "/intake/batches"
        return BatchView.model_validate(await self._json("POST", path, json=manifest.model_dump(by_alias=True)))

    async def intake_clients(self) -> tuple[IntakeClient, ...]:
        return tuple(IntakeClient.model_validate(row) for row in await self._json("GET", "/intake/clients"))

    async def create_client(self, client: NewClient) -> IntakeClient:
        raise ReviewError(409, "Add clients in the firm's client directory.")

    async def assign_slot(self, batch_id: str, slot: str, assignment: ClientAssignment) -> BatchView:
        payload = await self._json("PUT", f"/intake/batches/{batch_id}/files/{slot}/assignment",
                                   json=assignment.model_dump(by_alias=True))
        return BatchView.model_validate(payload)

    async def upload(
        self, batch_id: str, slot: str, media_type: str, content: bytes, return_id: str | None = None,
    ) -> BatchView:
        path = f"/returns/{return_id}/intake/batches" if return_id else "/intake/batches"
        return BatchView.model_validate(await self._json(
            "PUT", f"{path}/{batch_id}/files/{slot}", headers={"Content-Type": media_type}, content=content,
        ))

    async def exclude(self, batch_id: str, slot: str) -> BatchView:
        return BatchView.model_validate(await self._json("POST", f"/intake/batches/{batch_id}/files/{slot}/exclude"))

    async def open_review(self, access: ReviewAccess, revision: str) -> ReviewOpened:
        if self._native is None:
            raise ReviewError(409, "No licensed native review connector is enrolled on this machine.")
        return await self._native.open(access, revision)

    def session(self, access: ReviewAccess, session_id: str) -> ReviewSelectionSession:
        if self._native is None:
            raise ReviewUnavailable("No enrolled native connector.")
        return self._native.session(access, session_id)

    async def decision(self, access: ReviewAccess, command: ReviewDecision) -> ReviewDecisionRecorded:
        return ReviewDecisionRecorded.model_validate(await self._json(
            "POST", f"/returns/{access.return_id}/review/decisions", json=command.model_dump(by_alias=True),
        ))

    async def close_review(self, access: ReviewAccess, session_id: str) -> None:
        if self._native is None:
            raise ReviewError(409, "No licensed native review connector is enrolled on this machine.")
        await self._native.close(access, session_id)
