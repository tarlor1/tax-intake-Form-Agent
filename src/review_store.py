"""SQLite-backed human review state, evidence, and append-only event history."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .review_citations import citation_pages, match_citation
from .review_documents import (
    DocumentError,
    decode_text,
    extract_region,
    inspect_document,
    literal_citation,
    source_pages,
    validate_rect,
)
from .intake_contract import normalize_value
from .review_returns import blank_return, migrate_return


class ReviewError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _event(detail: dict, kind: str, field_id: str | None, message: str) -> None:
    detail["events"].append(
        {
            "id": uuid.uuid4().hex,
            "createdAt": _now(),
            "kind": kind,
            "fieldId": field_id,
            "message": message,
        }
    )


def _field(detail: dict, field_id: str) -> dict:
    for item in detail["fields"]:
        if item["id"] == field_id:
            return item
    raise ReviewError(404, "Field not found in this return.")


def _document_title(filename: str) -> str:
    if not isinstance(filename, str):
        raise ReviewError(422, "A document filename is required.")
    basename = re.split(r"[/\\]", filename)[-1]
    title = "".join(
        character for character in basename if not unicodedata.category(character).startswith("C")
    ).strip()
    if not title or title in {".", ".."}:
        raise ReviewError(422, "A nonempty document filename is required.")
    return title[:200]


def _proposal_value(item: dict, value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ReviewError(422, "A nonempty proposal value of at most 500 characters is required.")
    try:
        normalized = normalize_value(item, value)
    except ValueError as error:
        raise ReviewError(422, str(error)) from error
    if normalized is None:
        raise ReviewError(422, "A nonempty proposal value is required.")
    return normalized


def _source_pdf(connection: sqlite3.Connection, return_id: str, document_id: str) -> bytes:
    row = connection.execute(
        "SELECT pdf FROM review_documents WHERE return_id = ? AND document_id = ?",
        (return_id, document_id),
    ).fetchone()
    if row is None:
        raise ReviewError(404, "Source PDF is unavailable.")
    return bytes(row["pdf"])


def _evidence_id(prefix: str, identity: list) -> str:
    digest = hashlib.sha256(
        json.dumps(identity, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    return f"{prefix}-{digest}"


def _candidate_origin(candidate: dict) -> str:
    return (
        "ai"
        if candidate.get("provenance") == "gemini"
        and not any(not citation.get("localMatch") for citation in candidate.get("citations", []))
        else "source"
    )


def _apply_proposal(detail: dict, item: dict, candidate: dict) -> None:
    """Keep draft provenance and retain human decisions when new proposals arrive."""
    item["candidates"].append(candidate)
    value = candidate["value"]
    origin = _candidate_origin(candidate)
    description = "AI draft" if origin == "ai" else "Source-backed proposal"
    human = item.get("valueOrigin") == "preparer" or item.get("correctionReason") is not None
    if (item["value"] is None or item["value"] == "") and not human:
        item.update(
            value=value,
            citations=copy.deepcopy(candidate["citations"]),
            selectedCandidateId=candidate["id"],
            conflict=False,
            status="needs_review",
            valueOrigin=origin,
        )
        _event(
            detail,
            "evidence_proposed",
            item["id"],
            f"{description} for {item['label']} is ready for human review.",
        )
    elif item["value"] != value:
        item.update(conflict=True, status="blocked")
        _event(
            detail,
            "evidence_conflict",
            item["id"],
            f"New proposal {value!r} differs from {item['label']} ({item['value']!r}). Choose a proposal or save a preparer correction.",
        )
    else:
        if item.get("valueOrigin") == "workspace" and not human:
            item.update(
                citations=copy.deepcopy(candidate["citations"]),
                selectedCandidateId=candidate["id"],
                valueOrigin=origin,
                status="needs_review",
            )
        # A source decision and a preparer correction have intentional evidence boundaries.
        # Corroboration must not silently select a different source or re-tag a correction.
        _event(
            detail,
            "evidence_corroborated",
            item["id"],
            f"Additional proposal agrees with {item['label']}; existing review decision retained.",
        )


class ReviewStore:
    """Each mutation checks the return revision inside a single write transaction.

    Source PDFs are immutable SQLite blobs, not user-controlled filesystem paths.
    Existing state is never reset, populated with fixtures, or deleted at startup.
    """

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.data_dir / "review.sqlite3"
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS review_returns (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_documents (
                    return_id TEXT NOT NULL, document_id TEXT NOT NULL, pdf BLOB NOT NULL,
                    PRIMARY KEY (return_id, document_id)
                );
                CREATE TABLE IF NOT EXISTS review_intake_documents (
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE, content BLOB NOT NULL,
                    media_type TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_intake_identities (
                    identity_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, return_type TEXT NOT NULL
                );
            """)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _load(connection: sqlite3.Connection, return_id: str) -> dict:
        row = connection.execute(
            "SELECT payload FROM review_returns WHERE id = ?", (return_id,)
        ).fetchone()
        if row is None:
            raise ReviewError(404, "Return not found.")
        detail = migrate_return(json.loads(row["payload"]))
        for item in detail["fields"]:
            if "valueOrigin" not in item:
                item["valueOrigin"] = (
                    "calculated"
                    if item.get("dependencies")
                    else "preparer"
                    if item.get("correctionReason") is not None
                    else "source"
                    if item.get("citations")
                    else None
                )
        for document in detail["documents"]:
            document.setdefault("mediaType", "application/pdf")
        return detail

    def _public_return(self, detail: dict) -> dict:
        # Historical payload keys remain in storage, not in the current API contract.
        keys = (
            "id",
            "clientId",
            "clientName",
            "taxYear",
            "returnType",
            "destination",
            "revision",
            "activeFieldId",
            "fields",
            "legacyFields",
            "intakeContractVersion",
            "documents",
            "events",
        )
        result = copy.deepcopy({key: detail[key] for key in keys})
        self._presentation_citations(result)
        return result

    def _presentation_citations(self, detail: dict) -> None:
        """Enrich a detached API view without changing decisions or stored signatures."""
        pending = [
            (item, candidate)
            for item in detail["fields"]
            for candidate in item.get("candidates", [])
            if candidate.get("provenance") == "gemini" and not candidate.get("citations")
        ]
        if not pending:
            return
        documents = {document["id"]: document for document in detail["documents"]}
        with self._connection() as connection:
            for document_id in {candidate.get("documentId") for _, candidate in pending}:
                document = documents.get(document_id)
                if document is None:
                    continue
                row = connection.execute(
                    "SELECT pdf FROM review_documents WHERE return_id = ? AND document_id = ?",
                    (detail["id"], document_id),
                ).fetchone()
                if row is None:
                    continue
                content = bytes(row["pdf"])
                digest = hashlib.sha256(content).hexdigest()
                inbox = connection.execute(
                    "SELECT payload FROM review_intake_documents WHERE digest = ?", (digest,)
                ).fetchone()
                contexts = json.loads(inbox["payload"]).get("_extractedGroups", []) if inbox else []
                # The original may have been attached directly, before inbox groups
                # existed. Include every workspace sharing these immutable bytes.
                for shared in connection.execute(
                    "SELECT r.payload FROM review_returns r JOIN review_documents d "
                    "ON d.return_id = r.id WHERE d.pdf = ?", (content,)
                ):
                    saved = json.loads(shared["payload"])
                    contexts.append({
                        "clientName": saved["clientName"], "taxYear": saved["taxYear"],
                    })
                media_type = document.get("mediaType", "application/pdf")
                try:
                    pages = citation_pages(content, media_type)
                except DocumentError:
                    continue
                for item, candidate in pending:
                    if candidate.get("documentId") != document_id:
                        continue
                    grounded = match_citation(
                        pages, field=item, value=candidate["value"], media_type=media_type,
                        client_name=detail["clientName"], tax_year=detail["taxYear"],
                        contexts=contexts,
                    )
                    if grounded is None:
                        continue
                    location = (
                        f"lines {grounded['lineStart']}–{grounded['lineEnd']}"
                        if media_type == "text/plain" else f"page {grounded['page']}"
                    )
                    citation = {
                        "id": _evidence_id("local-citation", [
                            detail["id"], item["id"], candidate["id"], digest, grounded,
                        ]),
                        "documentId": document_id,
                        **grounded,
                        "label": f"{document['title']}, {location}",
                        "localMatch": True,
                    }
                    candidate["citations"] = [citation]
                    if (
                        item.get("selectedCandidateId") == candidate["id"]
                        and item.get("value") == candidate["value"]
                        and item.get("valueOrigin") == "ai"
                        and not item.get("citations")
                    ):
                        item["citations"] = [copy.deepcopy(citation)]

    def create_return(
        self,
        tax_year: int,
        *,
        client_id: str | None = None,
        client_name: str | None = None,
        return_type: str | None = None,
    ) -> dict:
        from datetime import date

        if type(tax_year) is not int or not 1990 <= tax_year <= date.today().year:
            raise ReviewError(
                422, f"taxYear must be an integer from 1990 through {date.today().year}."
            )
        if client_id is not None:
            if (
                not isinstance(client_id, str)
                or not client_id.strip()
                or len(client_id) > 150
                or client_name is not None
                or return_type is not None
            ):
                raise ReviewError(422, "Supply only clientId and taxYear for an existing client.")
        elif (
            not isinstance(client_name, str)
            or not client_name.strip()
            or len(client_name) > 200
            or return_type not in ("individual", "business")
        ):
            raise ReviewError(
                422, "A clientName and individual or business returnType are required."
            )
        with self._connection() as connection:
            # The identity lookup and duplicate guard share the write lock with insertion.
            connection.execute("BEGIN IMMEDIATE")
            if client_id is not None:
                saved = [
                    json.loads(row["payload"])
                    for row in connection.execute(
                        "SELECT payload FROM review_returns ORDER BY rowid"
                    )
                ]
                matches = [detail for detail in saved if detail["clientId"] == client_id]
                if not matches:
                    raise ReviewError(404, "Client not found.")
                if any(detail["taxYear"] == tax_year for detail in matches):
                    raise ReviewError(409, "This client already has a return for that tax year.")
                client_name = matches[0]["clientName"]
                return_type = matches[0]["returnType"]
            else:
                client_id = f"client-{uuid.uuid4().hex}"
                client_name = client_name.strip()
            detail = blank_return(
                f"return-{uuid.uuid4().hex}", client_id, client_name, tax_year, return_type
            )
            connection.execute(
                "INSERT INTO review_returns (id, payload, updated_at) VALUES (?, ?, ?)",
                (detail["id"], json.dumps(detail), _now()),
            )
            connection.commit()
            return detail

    def list_returns(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, updated_at FROM review_returns ORDER BY rowid"
            ).fetchall()
            details = [(self._load(connection, row["id"]), row["updated_at"]) for row in rows]
        summaries = []
        for detail, updated_at in details:
            summaries.append(
                {
                    **{
                        key: detail[key]
                        for key in (
                            "id",
                            "clientId",
                            "clientName",
                            "taxYear",
                            "returnType",
                            "destination",
                        )
                    },
                    "totalFields": len(detail["fields"]),
                    "acceptedFields": sum(
                        item["status"] == "accepted" for item in detail["fields"]
                    ),
                    "conflictFields": sum(item["conflict"] for item in detail["fields"]),
                    "missingFields": sum(
                        item["value"] is None or item["value"] == "" for item in detail["fields"]
                    ),
                    "updatedAt": updated_at,
                }
            )
        return summaries

    def get_return(self, return_id: str) -> dict:
        with self._connection() as connection:
            return self._public_return(self._load(connection, return_id))

    def get_pdf(self, return_id: str, document_id: str) -> bytes:
        detail = self.get_return(return_id)
        document = next((item for item in detail["documents"] if item["id"] == document_id), None)
        if (
            document is not None
            and document.get("mediaType", "application/pdf") != "application/pdf"
        ):
            raise ReviewError(415, "This source is TXT; use the text document endpoint.")
        with self._connection() as connection:
            detail = self._load(connection, return_id)
            if document_id not in {document["id"] for document in detail["documents"]}:
                raise ReviewError(404, "Document not found in this return.")
            row = connection.execute(
                "SELECT pdf FROM review_documents WHERE return_id = ? AND document_id = ?",
                (return_id, document_id),
            ).fetchone()
            if row is None:
                raise ReviewError(404, "Source PDF is unavailable.")
            return bytes(row["pdf"])

    def get_text(self, return_id: str, document_id: str) -> str:
        with self._connection() as connection:
            detail = self._load(connection, return_id)
            document = next(
                (item for item in detail["documents"] if item["id"] == document_id), None
            )
            if document is None:
                raise ReviewError(404, "Document not found in this return.")
            if document.get("mediaType", "application/pdf") != "text/plain":
                raise ReviewError(415, "This source is PDF; use the PDF document endpoint.")
            return decode_text(_source_pdf(connection, return_id, document_id))

    def _mutate(
        self,
        return_id: str,
        expected_revision: int,
        change: Callable[[dict, sqlite3.Connection], None],
    ) -> dict:
        if type(expected_revision) is not int or expected_revision < 1:
            raise ReviewError(422, "expectedRevision must be a positive integer.")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            detail = self._load(connection, return_id)
            if detail["revision"] != expected_revision:
                raise ReviewError(
                    409, "This return changed in another view. Reload before saving your review."
                )
            before = copy.deepcopy(detail)
            change(detail, connection)
            if detail != before:
                for item, old_item in zip(detail["fields"], before["fields"]):
                    if item != old_item:
                        item["revision"] = old_item["revision"] + 1
                detail["revision"] += 1
                connection.execute(
                    "UPDATE review_returns SET payload = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(detail), _now(), return_id),
                )
            connection.commit()
            return self._public_return(detail)

    def select(self, return_id: str, expected_revision: int, field_id: str) -> dict:
        def change(detail: dict, connection: sqlite3.Connection) -> None:
            target = _field(detail, field_id)
            if detail["activeFieldId"] == target["id"]:
                return
            detail["activeFieldId"] = target["id"]
            _event(
                detail,
                "field_selected",
                target["id"],
                f"Focused {target['label']} without accepting the previous field.",
            )

        return self._mutate(return_id, expected_revision, change)

    def add_document(
        self,
        return_id: str,
        expected_revision: int,
        filename: str,
        pdf: bytes,
        media_type: str = "application/pdf",
    ) -> dict:
        def change(detail: dict, connection: sqlite3.Connection) -> None:
            title = _document_title(filename)
            try:
                inspection = inspect_document(pdf, media_type)
            except DocumentError as error:
                raise ReviewError(error.status_code, error.detail) from error
            rows = connection.execute(
                "SELECT document_id, pdf FROM review_documents WHERE return_id = ?", (return_id,)
            ).fetchall()
            existing_id = next(
                (
                    row["document_id"]
                    for row in rows
                    if hashlib.sha256(row["pdf"]).hexdigest() == inspection["digest"]
                ),
                None,
            )
            if existing_id is not None:
                if any(document["id"] == existing_id for document in detail["documents"]):
                    return
                # A legacy database can contain immutable PDFs without visible metadata.
                # Promote the original blob using this upload's inspected metadata.
                document = {
                    "id": existing_id,
                    "title": title,
                    "pageCount": inspection["pageCount"],
                    "version": 1,
                    "mediaType": media_type,
                }
            else:
                document = {
                    "id": f"upload-{uuid.uuid4().hex}",
                    "title": title,
                    "pageCount": inspection["pageCount"],
                    "version": 1,
                    "mediaType": media_type,
                }
                connection.execute(
                    "INSERT INTO review_documents (return_id, document_id, pdf) VALUES (?, ?, ?)",
                    (return_id, document["id"], pdf),
                )
            detail["documents"].append(document)
            _event(
                detail,
                "document_added",
                None,
                f"Saved original source: {document['title']}. Extract supported fields or enter preparer values.",
            )

        return self._mutate(return_id, expected_revision, change)

    def add_proposal(
        self,
        return_id: str,
        expected_revision: int,
        field_id: str,
        document_id: str,
        page: int,
        rect: list[float],
        value: str,
        reason: str = "",
    ) -> dict:
        def change(detail: dict, connection: sqlite3.Connection) -> None:
            item = _field(detail, field_id)
            if item["dependencies"] or item["derivation"]:
                raise ReviewError(
                    422, "Calculated fields cannot receive manual proposals; review their inputs."
                )
            document = next(
                (source for source in detail["documents"] if source["id"] == document_id), None
            )
            if document is None:
                raise ReviewError(404, "Document not found in this return.")
            proposed_value = _proposal_value(item, value)
            if not isinstance(reason, str) or len(reason) > 2000:
                raise ReviewError(422, "The optional reason must be at most 2000 characters.")
            pdf = _source_pdf(connection, return_id, document_id)
            try:
                normalized_rect = validate_rect(rect)
                if document.get("mediaType", "application/pdf") == "text/plain":
                    pages = source_pages(pdf, "text/plain")
                    first = max(1, int(normalized_rect[1] * len(pages[0]["lines"])) + 1)
                    last = min(
                        len(pages[0]["lines"]),
                        max(
                            first,
                            int(
                                (normalized_rect[1] + normalized_rect[3]) * len(pages[0]["lines"])
                                + 0.999999
                            ),
                        ),
                    )
                    quote = "\n".join(line["text"] for line in pages[0]["lines"][first - 1 : last])
                    grounded = literal_citation(
                        pages,
                        page=page,
                        line_start=first,
                        line_end=last,
                        quote=quote,
                        media_type="text/plain",
                    )
                else:
                    quote = extract_region(pdf, page, normalized_rect)
                    grounded = {"page": page, "rect": normalized_rect, "quote": quote}
            except DocumentError as error:
                raise ReviewError(error.status_code, error.detail) from error
            identity = [
                return_id,
                detail["clientId"],
                detail["taxYear"],
                field_id,
                document_id,
                document["version"],
                hashlib.sha256(pdf).hexdigest(),
                page,
                normalized_rect,
            ]
            candidate_id = _evidence_id("human-candidate", identity + [proposed_value])
            if any(candidate["id"] == candidate_id for candidate in item["candidates"]):
                return
            citation = {
                "id": _evidence_id("human-citation", identity),
                "documentId": document_id,
                **grounded,
                "label": f"{document['title']}, page {page}, selected region",
            }
            candidate = {
                "id": candidate_id,
                "value": proposed_value,
                "citations": [citation],
                "provenance": "human",
                "documentId": document_id,
                "reason": reason.strip(),
                "documentVersion": document["version"],
            }
            _event(
                detail,
                "manual_proposal",
                field_id,
                f"Human supplied {proposed_value!r} for {item['label']}. Reason: {reason.strip()}. The quote is literal PDF text; its meaning and relationship to this value require human review, not automatic extraction.",
            )
            _apply_proposal(detail, item, candidate)

        return self._mutate(return_id, expected_revision, change)

    def resolve(
        self,
        return_id: str,
        expected_revision: int,
        field_id: str,
        candidate_id: str,
        reason: str = "",
    ) -> dict:
        def change(detail: dict, connection: sqlite3.Connection) -> None:
            if not isinstance(reason, str) or len(reason) > 2000:
                raise ReviewError(
                    422, "The optional source-selection reason must be at most 2000 characters."
                )

            item = _field(detail, field_id)
            if item["dependencies"]:
                raise ReviewError(
                    422, "Calculated fields cannot be resolved directly; review their inputs."
                )
            chosen = next(
                (candidate for candidate in item["candidates"] if candidate["id"] == candidate_id),
                None,
            )
            if chosen is None:
                raise ReviewError(422, "Candidate does not belong to this field and return.")

            old_value = item["value"]
            item.update(
                value=chosen["value"],
                citations=copy.deepcopy(chosen["citations"]),
                selectedCandidateId=chosen["id"],
                conflict=False,
                status="needs_review",
                correctionReason=None,
                valueOrigin=_candidate_origin(chosen),
            )
            if chosen["citations"]:
                selection = chosen["citations"][0]["label"]
            else:
                document = next(
                    (
                        source
                        for source in detail["documents"]
                        if source["id"] == chosen.get("documentId")
                    ),
                    None,
                )
                selection = f"AI draft from {document['title']}" if document else "AI draft"
            _event(
                detail,
                "source_resolved",
                field_id,
                f"Selected {selection} for {item['label']}; proposed value {old_value!r} -> {item['value']!r}. Reason: {reason.strip()}.",
            )

        return self._mutate(return_id, expected_revision, change)

    def correct(
        self,
        return_id: str,
        expected_revision: int,
        field_id: str,
        value: str,
        citation_ids: list[str],
        reason: str = "",
    ) -> dict:
        def change(detail: dict, connection: sqlite3.Connection) -> None:
            item = _field(detail, field_id)
            if item["dependencies"]:
                raise ReviewError(
                    422, "Calculated fields are read-only; correct the underlying inputs."
                )
            if value is not None and (not isinstance(value, str) or len(value) > 10_000):
                raise ReviewError(
                    422, "A correction value of at most 10000 characters is required."
                )
            if not isinstance(reason, str) or len(reason) > 2000:
                raise ReviewError(
                    422, "The optional correction reason must be at most 2000 characters."
                )

            try:
                corrected_value = normalize_value(item, value)
            except ValueError as error:
                raise ReviewError(422, str(error)) from error
            if (
                item["value"] == corrected_value
                and item.get("valueOrigin") == "preparer"
                and not citation_ids
                and item.get("correctionReason") == reason.strip()
            ):
                return

            reference_item = (
                _field(self._public_return(detail), field_id) if citation_ids else item
            )
            allowed = {
                citation["id"]: citation
                for candidate in reference_item["candidates"]
                for citation in candidate["citations"]
            }
            if not isinstance(citation_ids, list) or any(
                not isinstance(citation_id, str) or citation_id not in allowed
                for citation_id in citation_ids
            ):
                raise ReviewError(
                    422, "Documentary references must belong to this field and return."
                )

            old_value = item["value"]
            item.update(
                value=corrected_value,
                selectedCandidateId=None,
                conflict=False,
                citations=[
                    copy.deepcopy(allowed[citation_id])
                    for citation_id in dict.fromkeys(citation_ids)
                ],
                correctionReason=reason.strip(),
                valueOrigin="preparer",
                status="needs_review" if corrected_value is not None else "blocked",
            )
            _event(
                detail,
                "manual_correction",
                field_id,
                f"Human correction of {item['label']}: {old_value!r} -> {corrected_value!r}. Reason: {reason.strip()}. Selected documentary references provide context, not proof of the edited value.",
            )

        return self._mutate(return_id, expected_revision, change)

    def import_extracted_group(
        self,
        connection: sqlite3.Connection,
        detail: dict,
        *,
        filename: str,
        content: bytes,
        media_type: str,
        values: list[dict],
        pages: list[dict],
    ) -> dict:
        """Attach original documents and import AI drafts or legacy literal evidence."""
        before = copy.deepcopy(detail)
        digest = hashlib.sha256(content).hexdigest()
        rows = connection.execute(
            "SELECT document_id, pdf FROM review_documents WHERE return_id = ?", (detail["id"],)
        ).fetchall()
        document_id = next(
            (
                row["document_id"]
                for row in rows
                if hashlib.sha256(row["pdf"]).hexdigest() == digest
            ),
            None,
        )
        document = next((item for item in detail["documents"] if item["id"] == document_id), None)
        if document is None:
            document_id = document_id or f"upload-{uuid.uuid4().hex}"
            document = {
                "id": document_id,
                "title": _document_title(filename),
                "mediaType": media_type,
                "pageCount": len(pages),
                "version": 1,
            }
            connection.execute(
                "INSERT OR IGNORE INTO review_documents (return_id, document_id, pdf) VALUES (?, ?, ?)",
                (detail["id"], document_id, content),
            )
            detail["documents"].append(document)
            _event(detail, "document_added", None, f"Saved original source: {document['title']}.")

        for extracted in values:
            item = _field(detail, extracted["fieldId"])
            if item["dependencies"]:
                raise ReviewError(422, "Extraction cannot supply calculated fields.")
            try:
                value = normalize_value(item, extracted["value"])
            except ValueError as error:
                raise ReviewError(422, str(error)) from error
            if value is None:
                raise ReviewError(422, "A nonempty extracted draft value is required.")
            evidence = extracted.get("evidence")
            grounded = None
            if isinstance(evidence, dict):
                try:
                    grounded = literal_citation(
                        pages,
                        page=evidence.get("page"),
                        line_start=evidence.get("lineStart"),
                        line_end=evidence.get("lineEnd"),
                        quote=evidence.get("quote"),
                        media_type=media_type,
                    )
                except DocumentError:
                    # Optional source references must never reject a usable draft.
                    pass
            identity = [
                detail["id"],
                detail["clientId"],
                detail["taxYear"],
                item["id"],
                document_id,
                digest,
                grounded,
            ]
            candidate_id = _evidence_id("gemini-candidate", identity + [value])
            if any(candidate["id"] == candidate_id for candidate in item["candidates"]):
                continue
            citations = []
            if grounded is not None:
                citation = {
                    "id": _evidence_id("gemini-citation", identity),
                    "documentId": document_id,
                    **grounded,
                    "label": f"{document['title']}, page {grounded['page']}",
                }
                if media_type == "text/plain":
                    citation["label"] = (
                        f"{document['title']}, lines {grounded['lineStart']}–{grounded['lineEnd']}"
                    )
                citations.append(citation)
            _apply_proposal(
                detail,
                item,
                {
                    "id": candidate_id,
                    "value": value,
                    "citations": citations,
                    "provenance": "gemini",
                    "documentId": document_id,
                    "documentVersion": document["version"],
                },
            )

        document["extractionStatus"] = "completed"
        if detail != before:
            for item, old in zip(detail["fields"], before["fields"]):
                if item != old:
                    item["revision"] = old["revision"] + 1
            detail["revision"] += 1
            connection.execute(
                "UPDATE review_returns SET payload = ?, updated_at = ? WHERE id = ?",
                (json.dumps(detail), _now(), detail["id"]),
            )
        return detail
