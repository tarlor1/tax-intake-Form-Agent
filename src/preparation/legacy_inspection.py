"""Inspect saved prototype evidence without promoting values or restarting old jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import closing, contextmanager
from hashlib import sha256
from pathlib import Path
import sqlite3

from pydantic import ValidationError

from ..review_store import ReviewError, ReviewStore
from .contracts import DocumentEvidence, LineEvidence, OriginalRef, RegionEvidence
from .review_events import ReviewAccess, ReviewSelectionSession, ReviewUnavailable
from .review_routes import (EvidenceCandidate, EvidenceCitation, EvidenceDetail, EvidenceDocument,
                            EvidenceField, MediaType, ReviewDecision, ReviewDecisionRecorded, ReviewOpened, RevisionLink)

_UNAVAILABLE = "Archived intake evidence. No licensed Sigma profile is enabled in this workspace."


def _opaque(*parts: object) -> str:
    return sha256(str(parts).encode()).hexdigest()


class LegacyInspection:
    """Local archives use file permissions, not the firm's preparer identity system."""

    def __init__(self, data_dir: Path | None):
        self._path = Path(data_dir).expanduser().resolve() / "review.sqlite3" if data_dir is not None else None

    @contextmanager
    def _database(self) -> Generator[sqlite3.Connection | None, None, None]:
        if self._path is None or not self._path.is_file():
            yield None
            return
        with closing(sqlite3.connect(self._path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='review_returns' AND type='table'").fetchone() is None:
                yield None
                return
            with db:
                db.execute("BEGIN")
                yield db

    @staticmethod
    def _load(db: sqlite3.Connection | None, return_id: str, revision: str | None = None) -> dict:
        if db is None:
            raise ReviewError(404, "Return not found in the local archive.")
        detail = ReviewStore._load(db, return_id)
        if revision is not None and revision != LegacyInspection._revision(detail):
            raise ReviewError(404, "That revision is not available in the local archive.")
        return detail

    @staticmethod
    def _revision(detail: dict) -> str:
        return _opaque(detail["id"], detail["revision"])

    async def access(self, return_id: str) -> ReviewAccess:
        return ReviewAccess("local-archive", "local-inspector", return_id)

    async def returns(self) -> list[dict]:
        return await asyncio.to_thread(self._returns)

    def _returns(self) -> list[dict]:
        with self._database() as db:
            if db is None:
                return []
            summaries = []
            for row in db.execute("SELECT id,updated_at FROM review_returns ORDER BY rowid"):
                detail = self._load(db, row["id"])
                fields = (*detail["fields"], *detail["legacyFields"])
                summaries.append({
                    **{key: detail[key] for key in ("id", "clientId", "clientName", "taxYear", "returnType")},
                    "destination": "Archived intake", "totalFields": len(fields),
                    "acceptedFields": sum(item["status"] == "accepted" for item in fields),
                    "conflictFields": sum(item["conflict"] for item in fields),
                    "missingFields": sum(item.get("value") in (None, "") for item in fields),
                    "updatedAt": row["updated_at"], "reviewRequired": False,
                })
            return summaries

    async def evidence(self, access: ReviewAccess, revision: str | None) -> EvidenceDetail:
        return await asyncio.to_thread(self._evidence, access.return_id, revision)

    def _evidence(self, return_id: str, revision: str | None) -> EvidenceDetail:
        with self._database() as db:
            detail = self._load(db, return_id, revision)
        revision = self._revision(detail)
        documents = {
            item["id"]: EvidenceDocument(
                id=item["id"], version_id=str(item["version"]), title=item["title"],
                media_type=item["mediaType"], page_count=item.get("pageCount") if item["mediaType"] == "application/pdf" else None,
            )
            for item in detail["documents"]
        }
        fields = []
        field_ids = set()
        candidate_ids = set()
        for item in (*detail["fields"], *detail["legacyFields"]):
            if not (item.get("value") not in (None, "") or item.get("candidates") or item.get("citations") or item.get("required")):
                continue
            field_id = item["id"]
            occurrence = 0
            while field_id in field_ids:
                occurrence += 1
                field_id = _opaque("archive-field", item["id"], occurrence)
            field_ids.add(field_id)
            candidates, used = self._candidates(item, field_id, documents, candidate_ids)
            fields.append(EvidenceField(
                id=field_id, label=item["label"], record_label=item.get("section") or "Archived intake",
                value=item.get("value"), provenance="archived",
                status="conflict" if item.get("conflict") else "unverified",
                used_citation_id=used, candidates=candidates, derivation=item.get("derivation"),
            ))
        return EvidenceDetail(
            id=detail["id"], client_id=detail["clientId"], client_name=detail["clientName"],
            tax_year=detail["taxYear"], return_type=detail["returnType"], input_revision=revision,
            latest_revision=revision, revisions=(RevisionLink(id=revision, label="Archived intake"),),
            documents=tuple(documents.values()), fields=tuple(fields), review_available=False, review_message=_UNAVAILABLE,
        )

    @staticmethod
    def _candidates(
        item: dict, field_id: str, documents: dict[str, EvidenceDocument], seen: set[str],
    ) -> tuple[tuple[EvidenceCandidate, ...], str | None]:
        rows = list(item.get("candidates", ()))
        selected_id = item.get("selectedCandidateId")
        if not rows and item.get("citations"):
            selected_id = _opaque(item["id"], "archive")
            rows = [{"id": selected_id, "value": item.get("value"), "citations": item["citations"]}]
        result = []
        used = None
        selected_is_unique = sum(row["id"] == selected_id for row in rows) == 1
        for position, row in enumerate(rows):
            candidate_id = row["id"]
            if field_id != item["id"] or candidate_id in seen:
                candidate_id = _opaque("archive-candidate", field_id, position, candidate_id)
            seen.add(candidate_id)
            citations = []
            for index, raw in enumerate(row.get("citations", ())):
                doc = documents.get(raw.get("documentId"))
                declared_version = row.get("documentVersion")
                if doc is None or (declared_version is not None and str(declared_version) != doc.version_id):
                    continue
                original = OriginalRef(document_id=doc.id, version_id=doc.version_id)
                evidence = DocumentEvidence(precision="document", original=original)
                try:
                    if not raw.get("localMatch") and doc.media_type == "text/plain" and raw.get("lineStart") and raw.get("lineEnd"):
                        evidence = LineEvidence(precision="lines", original=original, line_start=raw["lineStart"],
                                                line_end=raw["lineEnd"], quote=raw["quote"])
                    elif (not raw.get("localMatch") and doc.page_count is not None and type(raw.get("page")) is int
                          and 1 <= raw["page"] <= doc.page_count and raw.get("rect")):
                        evidence = RegionEvidence(precision="region", original=original, page=raw["page"],
                                                  rect=raw["rect"], quote=raw["quote"])
                except (KeyError, ValidationError):
                    pass  # A malformed historical anchor cannot advertise a highlight.
                citations.append(EvidenceCitation(id=_opaque(field_id, candidate_id, index),
                                                  label=raw.get("label") or doc.title, evidence=evidence))
            original_id = row.get("documentId")
            if not citations and original_id in documents:
                doc = documents[original_id]
                if row.get("documentVersion") is None or str(row["documentVersion"]) == doc.version_id:
                    citations.append(EvidenceCitation(
                        id=_opaque(field_id, candidate_id, doc.version_id), label=doc.title,
                        evidence=DocumentEvidence(precision="document", original=OriginalRef(document_id=doc.id, version_id=doc.version_id)),
                    ))
            result.append(EvidenceCandidate(id=candidate_id, label="Archived candidate", value=row.get("value"), citations=tuple(citations)))
            if selected_is_unique and row["id"] == selected_id and not item.get("conflict") and citations:
                used = citations[0].id
        return tuple(result), used

    async def original(self, access: ReviewAccess, revision: str, document_id: str, version_id: str) -> tuple[MediaType, bytes]:
        return await asyncio.to_thread(self._original, access.return_id, revision, document_id, version_id)

    def _original(self, return_id: str, revision: str, document_id: str, version_id: str) -> tuple[MediaType, bytes]:
        with self._database() as db:
            detail = self._load(db, return_id, revision)
            document = next((item for item in detail["documents"]
                             if item["id"] == document_id and str(item["version"]) == version_id), None)
            if document is None or db is None:
                raise ReviewError(404, "Document version not found in this return revision.")
            row = db.execute("SELECT pdf FROM review_documents WHERE (return_id,document_id)=(?,?)",
                             (return_id, document_id)).fetchone()
            if row is None:
                raise ReviewError(404, "The archived original is unavailable.")
            return document["mediaType"], row["pdf"]

    async def jobs(self, return_id: str) -> tuple[()]:
        await asyncio.to_thread(self._job_inspection, return_id)
        return ()  # Historical prototype jobs are not preparation assignments.

    def _job_inspection(self, return_id: str) -> None:
        with self._database() as db:
            self._load(db, return_id)

    async def open_review(self, access: ReviewAccess, revision: str) -> ReviewOpened:
        raise ReviewError(409, _UNAVAILABLE)

    def session(self, access: ReviewAccess, session_id: str) -> ReviewSelectionSession:
        raise ReviewUnavailable(_UNAVAILABLE)

    async def decision(self, access: ReviewAccess, command: ReviewDecision) -> ReviewDecisionRecorded:
        raise ReviewError(409, _UNAVAILABLE)

    async def close_review(self, access: ReviewAccess, session_id: str) -> None:
        raise ReviewError(409, _UNAVAILABLE)
