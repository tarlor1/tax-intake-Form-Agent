"""Evidence-only HTTP surface. All native writes remain in the tax application."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Header, Query, Response
from pydantic import Field, StrictInt, model_validator

from ..review_store import ReviewError
from .contracts import Contract, Evidence
from .review_events import ReviewAccess, ReviewSelectionSession, ReviewStreamConflict, ReviewUnavailable
from .tool_contract import Ref

Count = Annotated[StrictInt, Field(ge=0)]
RevisionId = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")]
Status = Literal["verified", "unverified", "conflict", "needs_client_input"]
MediaType = Literal["application/pdf", "text/plain"]


class RevisionLink(Contract):
    id: Ref
    label: str


class EvidenceDocument(Contract):
    id: Ref
    version_id: Ref
    title: str
    media_type: MediaType
    page_count: Annotated[StrictInt, Field(ge=1)] | None


class EvidenceCitation(Contract):
    id: Ref
    label: str
    evidence: Evidence


class EvidenceCandidate(Contract):
    id: Ref
    label: str
    value: str | None
    citations: tuple[EvidenceCitation, ...] = ()


class EvidenceField(Contract):
    id: Ref
    label: str
    record_label: str
    value: str | None = None
    prior_source_value: str | None = None
    proposed_value: str | None = None
    saved_value: str | None = None
    provenance: Literal["source", "preparer", "archived", "none"] = "none"
    status: Status = "unverified"
    used_citation_id: Ref | None = None
    candidates: tuple[EvidenceCandidate, ...] = ()
    derivation: str | None = None


class EvidenceDetail(Contract):
    id: Ref
    client_id: Ref
    client_name: str
    tax_year: Annotated[StrictInt, Field(ge=1900, le=2200)]
    return_type: Literal["individual", "business", "1120"]
    input_revision: Ref
    latest_revision: Ref
    revisions: tuple[RevisionLink, ...]
    documents: tuple[EvidenceDocument, ...]
    fields: tuple[EvidenceField, ...]
    review_available: bool
    review_message: str | None = None
    decision_version: Count = 0
    field_version: Count = 0

    @model_validator(mode="after")
    def check_references(self):
        if self.review_available and "field_version" not in self.model_fields_set:
            raise ValueError("A reviewable detail requires its exact field snapshot version.")
        revisions = {revision.id for revision in self.revisions}
        documents = {(document.id, document.version_id): document for document in self.documents}
        if (
            not {self.input_revision, self.latest_revision} <= revisions or len(revisions) != len(self.revisions)
            or len(documents) != len(self.documents) or len({field.id for field in self.fields}) != len(self.fields)
        ):
            raise ValueError("Evidence detail has missing or ambiguous scoped identities.")
        for field in self.fields:
            ids = set()
            for candidate in field.candidates:
                for citation in candidate.citations:
                    evidence = citation.evidence
                    original = evidence.original
                    document = documents.get((original.document_id, original.version_id))
                    if document is None or citation.id in ids:
                        raise ValueError("A citation is ambiguous or outside this return revision.")
                    ids.add(citation.id)
                    if evidence.precision == "lines" and document.media_type != "text/plain":
                        raise ValueError("Line references require a TXT original.")
                    if evidence.precision in {"region", "page"} and (
                        document.media_type != "application/pdf" or document.page_count is None
                        or evidence.page > document.page_count
                    ):
                        raise ValueError("The cited PDF page is unavailable.")
            if field.used_citation_id is not None and field.used_citation_id not in ids:
                raise ValueError("Used citation is outside the field's candidates.")
        return self


class ReviewOpen(Contract):
    input_revision: Ref


class ReviewOpened(Contract):
    status: Literal["open", "queued", "recovery_required"]
    review_session_id: Ref | None
    return_id: Ref
    input_revision: Ref

    @model_validator(mode="after")
    def check_session(self):
        if (self.status != "queued") != (self.review_session_id is not None):
            raise ValueError("An open or recoverable review requires a local session.")
        return self


class ReviewClose(Contract):
    review_session_id: Ref


class ReviewDecision(Contract):
    kind: Literal["mark_reviewed", "choose_source"]
    review_session_id: Ref
    input_revision: Ref
    expected_decision_version: Count
    expected_field_version: Count
    binding_id: Ref | None = None
    candidate_id: Ref | None = None

    @model_validator(mode="after")
    def check_decision(self):
        candidate, binding = self.candidate_id, self.binding_id
        if self.kind == "choose_source" and (not candidate or not binding):
            raise ValueError("Choosing a source requires the exact binding and candidate.")
        if self.kind == "mark_reviewed" and (candidate or binding):
            raise ValueError("Sign-off applies to the input revision, not a candidate.")
        return self


class ReviewDecisionRecorded(Contract):
    decision_version: Count


class ReviewBackend(Protocol):
    """Trusted local backend. A loopback token alone does not create ReviewAccess."""

    async def access(self, return_id: str) -> ReviewAccess: ...

    async def evidence(self, access: ReviewAccess, revision: str | None) -> EvidenceDetail: ...

    async def original(
        self, access: ReviewAccess, revision: str, document_id: str, version_id: str,
    ) -> tuple[MediaType, bytes]: ...

    async def open_review(self, access: ReviewAccess, revision: str) -> ReviewOpened: ...

    def session(self, access: ReviewAccess, session_id: str) -> ReviewSelectionSession: ...

    async def decision(self, access: ReviewAccess, command: ReviewDecision) -> ReviewDecisionRecorded:
        """Record only if the field and decision versions still match atomically."""
        ...

    async def close_review(self, access: ReviewAccess, session_id: str) -> None:
        """Check reservation ownership and native close even after selection expiry."""
        ...


def review_router(backend: ReviewBackend) -> APIRouter:
    router = APIRouter(prefix="/api/review/returns")

    def active(access: ReviewAccess, session_id: str) -> ReviewSelectionSession:
        try:
            session = backend.session(access, session_id)
            session.require_access(access, session_id)
            return session
        except ReviewUnavailable:
            raise ReviewError(404, "Review session is not available in this return.") from None

    async def scoped_evidence(access: ReviewAccess, return_id: str, revision: str | None) -> EvidenceDetail:
        detail = await backend.evidence(access, revision)
        if detail.id != return_id or (revision is not None and revision != detail.input_revision):
            raise ReviewError(404, "Evidence revision not found in this return.")
        return detail

    @router.get("/{return_id}/evidence", response_model=EvidenceDetail)
    async def evidence(return_id: RevisionId, revision: Annotated[RevisionId | None, Query()] = None):
        access = await backend.access(return_id)
        return await scoped_evidence(access, return_id, revision)

    @router.get("/{return_id}/review/originals/{document_id}/{version_id}")
    async def original(return_id: RevisionId, document_id: RevisionId, version_id: RevisionId,
                       revision: Annotated[RevisionId, Query()]):
        access = await backend.access(return_id)
        media_type, content = await backend.original(access, revision, document_id, version_id)
        return Response(content, media_type=media_type)

    @router.post("/{return_id}/review/open", response_model=ReviewOpened)
    async def open_review(return_id: RevisionId, body: ReviewOpen):
        access = await backend.access(return_id)
        result = await backend.open_review(access, body.input_revision)
        if result.return_id != return_id or (
            result.status != "recovery_required" and result.input_revision != body.input_revision
        ):
            raise ReviewError(409, "The saved draft is not available for this revision.")
        if result.status == "open" and result.review_session_id is not None:
            if active(access, result.review_session_id).input_revision != result.input_revision:
                raise ReviewError(409, "The active native reservation belongs to another revision.")
        return result

    @router.get("/{return_id}/review/events")
    async def events(return_id: RevisionId, session_id: Annotated[RevisionId, Header(alias="X-Review-Session")]):
        access = await backend.access(return_id)
        session = active(access, session_id)
        try:
            return session.event_response(access, session_id)
        except ReviewStreamConflict as exc:
            raise ReviewError(409, str(exc)) from None
        except ReviewUnavailable:
            raise ReviewError(404, "Review session is not available in this return.") from None

    @router.post("/{return_id}/review/decisions", response_model=ReviewDecisionRecorded)
    async def decision(return_id: RevisionId, body: ReviewDecision):
        access = await backend.access(return_id)
        session = active(access, body.review_session_id)
        if session.input_revision != body.input_revision:
            raise ReviewError(409, "The decision does not apply to the active input revision.")
        detail = await scoped_evidence(access, return_id, body.input_revision)
        if ("field_version" not in detail.model_fields_set
                or detail.field_version != body.expected_field_version
                or detail.decision_version != body.expected_decision_version):
            raise ReviewError(409, "The evidence changed. Refresh and review the current field snapshot.")
        if body.kind == "choose_source" and not any(
            field.id == body.binding_id and any(candidate.id == body.candidate_id for candidate in field.candidates)
            for field in detail.fields
        ):
            raise ReviewError(409, "The candidate is outside this field snapshot.")
        if not session.active:
            raise ReviewError(404, "Review session is not available in this return.")
        return await backend.decision(access, body)

    @router.post("/{return_id}/review/close", status_code=204)
    async def close(return_id: RevisionId, body: ReviewClose):
        access = await backend.access(return_id)
        try:
            await backend.close_review(access, body.review_session_id)
        except ReviewUnavailable:
            raise ReviewError(404, "Review reservation is not available in this return.") from None
        return Response(status_code=204)

    return router
