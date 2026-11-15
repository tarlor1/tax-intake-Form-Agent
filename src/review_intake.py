"""Consent-gated normalized client/year drafts and durable document association."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .ai_client import EXTRACTION_SYSTEM
from .extraction import describe_validation_error
from .form_filler import form_values
from .intake_contract import CONTRACT_VERSION
from .models import IntakeExtraction
from .review_documents import DocumentError, source_pages
from .review_returns import blank_return
from .review_store import ReviewError, ReviewStore, _document_title, _now

DEFAULT_MODEL = "gemini-3.5-flash-lite"
MAX_ACTIVE_DOCUMENTS = 100
MAX_BATCH_DOCUMENTS = 100
MAX_RESPONSE_CHARS = 250_000
MAX_EXTRACTION_ATTEMPTS = 2


class ExtractedGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clientName: str | None = Field(default=None, max_length=200)
    returnType: Literal["individual", "business"] | None
    record: IntakeExtraction


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    groups: list[ExtractedGroup] = Field(min_length=1, max_length=100)


def _gemini_response_schema() -> dict:
    # The application retains all original normalization/validation. Keep the
    # generation grammar simple enough for Gemini's constrained decoder.
    schema = Extraction.model_json_schema()
    pending = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for keyword in ("minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum"):
                node.pop(keyword, None)
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return schema


INSTRUCTION = EXTRACTION_SYSTEM + """

MULTI-CLIENT AND MULTI-YEAR OUTPUT:
Apply the field rules independently to every client/entity and every requested tax year.
Return one groups entry for each distinct client and tax year, with its own normalized record.
This overrides the single-record rule to pick only the most recent year: include every year
the document requests. A shared contact, address, paragraph or sentence may describe several
records. No non-overlapping line ranges or verbatim answer quotes are required.
clientName identifies the taxpayer or business seeking preparation, not their employer or payer.
For businesses, clientName is the entity's legal name. returnType is individual or business.
Do not create profiles for people merely mentioned as employers, dependents or contacts.
Do not invent separate years from incidental birth dates, license dates or filing deadlines.
Normalize dates, states, choices, income categories and summaries using the original field rules.
Each record's tax_year must be the year belonging to that entry, not a different group's year.
Missing information is null/empty and remains available for preparer review.
All results are AI-extracted drafts. Do not claim that answers are independently verified.
"""


def _space(text: str) -> str:
    return " ".join(text.split()).casefold()


def validate_extraction(raw: str | dict | Extraction) -> list[dict]:
    """Use the original intake model for each detected client/year record."""
    try:
        if isinstance(raw, Extraction):
            result = raw
        elif isinstance(raw, str):
            if len(raw) > MAX_RESPONSE_CHARS:
                raise ValueError("oversized response")
            result = Extraction.model_validate_json(raw)
        else:
            result = Extraction.model_validate(raw)
    except (ValidationError, ValueError, TypeError) as error:
        raise ReviewError(
            422,
            "Gemini returned an invalid intake record. No new drafts were imported; review the source and retry.",
        ) from error

    groups = []
    for extracted in result.groups:
        record = extracted.record
        name = extracted.clientName if extracted.returnType == "business" else record.full_name
        name = name or extracted.clientName or record.full_name
        name = name.strip() if name else None
        values = []
        for field_id, value in form_values(record).items():
            if value == "" or value == []:
                continue
            if field_id == "full_name" and name:
                value = name
            canonical = json.dumps(value, separators=(",", ":")) if isinstance(value, list) else value
            values.append({"fieldId": field_id, "value": canonical})

        if name and not record.full_name:
            values.append({"fieldId": "full_name", "value": name})
        identity_hash = None
        if name and record.email:
            identity_hash = hashlib.sha256(
                json.dumps([
                    extracted.returnType, _space(name), record.email.casefold(),
                    record.date_of_birth.isoformat() if record.date_of_birth else None,
                ]).encode()
            ).hexdigest()
        groups.append({
            "id": f"group-{uuid.uuid4().hex}",
            "clientName": name,
            "taxYear": record.tax_year,
            "returnType": extracted.returnType,
            "identityHash": identity_hash,
            "values": values,
            "issues": [],
        })
    return groups


class ReviewIntake:
    def __init__(
        self, store: ReviewStore, *, mutation_lock, ensure_idle: Callable[[str], None],
        prepare_return: Callable[[str], None] | None = None,
    ):
        self.store = store
        self.mutation_lock = mutation_lock
        self.ensure_idle = ensure_idle
        self.prepare_return = prepare_return
        self._pending_returns: set[str] = set()
        self._lock = threading.RLock()
        self._active: set[str] = set()
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="review-intake")

        try:
            from dotenv import load_dotenv

            load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
        except ImportError:
            pass

        # Consent cannot survive an interrupted external operation. A user must explicitly retry.
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for row in connection.execute(
                "SELECT id, payload FROM review_intake_documents"
            ).fetchall():
                document = json.loads(row["payload"])
                if document.get("_intakeContractVersion") != CONTRACT_VERSION:
                    if (
                        document["status"] in {"completed", "needs_assignment"}
                        or document.get("groups")
                        or document.get("_extractedGroups")
                    ):
                        document.setdefault("_legacyExtractions", []).append(
                            {
                                "groups": document["groups"],
                                "extractedGroups": document.pop("_extractedGroups", []),
                            }
                        )
                        document.update(
                            status="uploaded",
                            groups=[],
                            error="The intake fields changed. Approve extraction again to populate the original form.",
                            updatedAt=_now(),
                        )
                    document["_intakeContractVersion"] = CONTRACT_VERSION

                if document["status"] == "processing":
                    document.update(
                        status="failed",
                        error="Extraction was interrupted. Approve the document again to retry.",
                        updatedAt=_now(),
                    )

                self._save(connection, document)
            connection.commit()

    def config(self) -> dict:
        return {
            "configured": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
            "model": os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL,
        }

    @staticmethod
    def _public(document: dict) -> dict:
        return copy.deepcopy(
            {key: value for key, value in document.items() if not key.startswith("_")}
        )

    @staticmethod
    def _save(connection, document: dict) -> None:
        connection.execute(
            "UPDATE review_intake_documents SET payload = ? WHERE id = ?",
            (json.dumps(document), document["id"]),
        )

    @staticmethod
    def _get(connection, document_id: str):
        row = connection.execute(
            "SELECT * FROM review_intake_documents WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None:
            raise ReviewError(404, "Inbox document not found.")
        return row, json.loads(row["payload"])

    def list_documents(self) -> list[dict]:
        with self.store._connection() as connection:
            return [
                self._public(json.loads(row["payload"]))
                for row in connection.execute(
                    "SELECT payload FROM review_intake_documents ORDER BY rowid"
                )
            ]

    def upload(self, filename: str, content: bytes, media_type: str) -> dict:
        title = _document_title(filename)
        try:
            source_pages(content, media_type)
        except DocumentError as error:
            raise ReviewError(error.status_code, error.detail) from error

        digest = hashlib.sha256(content).hexdigest()
        with self._lock, self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM review_intake_documents WHERE digest = ?", (digest,)
            ).fetchone()
            if row is not None:
                return self._public(json.loads(row["payload"]))
            now = _now()
            document = {
                "id": f"inbox-{uuid.uuid4().hex}",
                "filename": title,
                "mediaType": media_type,
                "status": "uploaded",
                "error": None,
                "groups": [],
                "createdAt": now,
                "updatedAt": now,
                "_intakeContractVersion": CONTRACT_VERSION,
            }
            connection.execute(
                "INSERT INTO review_intake_documents (id, digest, content, media_type, payload) VALUES (?, ?, ?, ?, ?)",
                (document["id"], digest, content, media_type, json.dumps(document)),
            )
            connection.commit()
            return self._public(document)

    def process(self, document_ids: list[str], approved_for_ai: bool) -> list[dict]:
        if (
            not isinstance(document_ids, list)
            or not document_ids
            or len(document_ids) > MAX_BATCH_DOCUMENTS
            or any(not isinstance(item, str) or not item for item in document_ids)
        ):
            raise ReviewError(422, f"Select between 1 and {MAX_BATCH_DOCUMENTS} inbox documents.")
        if approved_for_ai is not True:
            raise ReviewError(
                422,
                "Extraction requires explicit approval to send only synthetic or redacted documents to Gemini. No document was sent.",
            )

        with self._lock:
            if self._closed:
                raise ReviewError(
                    503, "Document intake is shutting down. Reopen the service to continue."
                )

            with self.store._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                documents = [self._get(connection, item)[1] for item in dict.fromkeys(document_ids)]
                pending = [
                    doc
                    for doc in documents
                    if doc["status"] not in {"processing", "completed", "needs_assignment"}
                ]

                if pending and not self.config()["configured"]:
                    for document in pending:
                        document.update(
                            status="failed",
                            error="Gemini is not configured. Set GEMINI_API_KEY in the project .env or environment, then retry.",
                            updatedAt=_now(),
                        )
                        self._save(connection, document)
                    connection.commit()
                    raise ReviewError(
                        503,
                        "Gemini is not configured. Set GEMINI_API_KEY in the project .env or environment, then retry. No document was sent.",
                    )
                if len(self._active) + len(pending) > MAX_ACTIVE_DOCUMENTS:
                    raise ReviewError(
                        429,
                        "The extraction queue is full. Wait for current documents to finish, then retry.",
                    )

                for document in pending:
                    document.update(status="processing", error=None, updatedAt=_now())
                    self._save(connection, document)
                connection.commit()

            self._active.update(document["id"] for document in pending)
            for document in pending:
                self._executor.submit(self._work, document["id"])
            return [self._public(document) for document in documents]

    def process_return(
        self, return_id: str, expected_revision: int, approved_for_ai: bool
    ) -> list[dict]:
        if approved_for_ai is not True:
            raise ReviewError(
                422,
                "Approve only synthetic or redacted documents before Gemini extraction. No document was sent.",
            )

        with self._lock, self.mutation_lock:
            self.ensure_idle(return_id)
            detail = self.store.get_return(return_id)
            if type(expected_revision) is not int or detail["revision"] != expected_revision:
                raise ReviewError(
                    409, "This return changed. Reload before extracting its documents."
                )

            documents = []
            with self.store._connection() as connection:
                for source in detail["documents"]:
                    if source.get("extractionStatus") == "completed":
                        continue
                    row = connection.execute(
                        "SELECT pdf FROM review_documents WHERE return_id = ? AND document_id = ?",
                        (return_id, source["id"]),
                    ).fetchone()
                    if row is None:
                        raise ReviewError(404, "An attached source document is unavailable.")
                    documents.append(
                        self.upload(
                            source["title"],
                            bytes(row["pdf"]),
                            source.get("mediaType", "application/pdf"),
                        )
                    )

            if not documents:
                raise ReviewError(
                    422, "No unextracted source documents are attached to this return."
                )
            return self.process([document["id"] for document in documents], approved_for_ai)

    def _call_gemini(self, pages: list[dict], previous_error: str | None = None) -> str:
        try:
            from google import genai
            from google.genai import errors, types
        except ImportError as error:
            raise ReviewError(
                503,
                "Gemini support is not installed. Install review-requirements.txt and restart the service.",
            ) from error

        # JSON string framing keeps document text distinct from trusted instructions.
        payload = json.dumps(
            {
                "today": date.today().isoformat(),
                "previousValidationError": previous_error,
                "untrustedDocumentPages": [
                    {
                        "page": page["page"],
                        "lines": [
                            {"number": index, "text": line["text"]}
                            for index, line in enumerate(page["lines"], 1)
                        ],
                    }
                    for page in pages
                ]
            },
            ensure_ascii=False,
        )

        try:
            with genai.Client(
                api_key=os.environ.get("GEMINI_API_KEY"),
                http_options=types.HttpOptions(
                    timeout=60_000, retry_options=types.HttpRetryOptions(attempts=1)
                ),
            ) as client:
                response = client.models.generate_content(
                    model=self.config()["model"],
                    contents=payload,
                    config=types.GenerateContentConfig(
                        system_instruction=INSTRUCTION,
                        temperature=0,
                        max_output_tokens=16_384,
                        response_mime_type="application/json",
                        response_json_schema=_gemini_response_schema(),
                    ),
                )
                text = response.text
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_RESPONSE_CHARS:
                raise ReviewError(
                    422,
                    "Gemini returned no usable extraction or an oversized response. No values were imported.",
                )
            if os.environ.get("GEMINI_DEBUG", "").strip() == "1":
                # JSON escaping prevents untrusted response text from injecting terminal controls.
                logging.getLogger(__name__).warning(
                    "GEMINI_DEBUG raw response (may contain sensitive document data): %s",
                    json.dumps({"response": text}, ensure_ascii=True),
                )
            return text
        except ReviewError:
            raise
        except errors.APIError as error:
            if error.code == 400:
                action = "Google rejected the request. Check the extraction request and schema configuration."
            elif error.code in {401, 403}:
                action = "Google denied access. Check the API key's project permissions and model access."
            elif error.code == 404:
                action = "The configured Gemini model was not found."
            elif error.code == 429:
                action = "Google reported a quota or rate limit. Check the project's quota before retrying."
            else:
                action = "Google could not complete the request. Try again later."

            # Report only the status and fixed guidance, never a raw provider payload.
            raise ReviewError(
                502,
                f"Gemini extraction failed (HTTP {error.code}). {action} No new values were imported.",
            ) from None
        except Exception:
            # Provider exceptions can contain credentials, request URLs or source content.
            raise ReviewError(
                502,
                "Gemini extraction failed. Check the API key, model access, quota and network, then retry. No new values were imported.",
            ) from None

    def _work(self, document_id: str) -> None:
        try:
            with self._lock:
                if self._closed:
                    raise ReviewError(
                        503,
                        "Extraction stopped during service shutdown. Approve the document again to retry.",
                    )

            with self.store._connection() as connection:
                row, document = self._get(connection, document_id)
                content, media_type = bytes(row["content"]), row["media_type"]
            if not self.config()["configured"]:
                raise ReviewError(
                    503,
                    "Gemini is not configured. Set GEMINI_API_KEY and retry. No document was sent.",
                )

            try:
                pages = source_pages(content, media_type)
            except DocumentError as error:
                raise ReviewError(error.status_code, error.detail) from error
            feedback = None
            for attempt in range(MAX_EXTRACTION_ATTEMPTS):
                raw = self._call_gemini(pages) if feedback is None else self._call_gemini(pages, feedback)
                try:
                    groups = validate_extraction(raw)
                    break
                except ReviewError as error:
                    if attempt + 1 == MAX_EXTRACTION_ATTEMPTS:
                        raise
                    cause = error.__cause__
                    feedback = describe_validation_error(cause) if isinstance(cause, ValidationError) else error.detail

            with self.mutation_lock, self.store._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                _, document = self._get(connection, document_id)
                document["_extractedGroups"] = groups
                document["groups"] = []
                for group in groups:
                    public = {
                        "id": group["id"],
                        "clientName": group["clientName"],
                        "taxYear": group["taxYear"],
                        "returnType": group["returnType"],
                        "status": "needs_assignment",
                        "returnId": None,
                        "clientId": None,
                        "issues": list(group["issues"]),
                    }
                    missing = [
                        label
                        for label, value in (
                            ("client name", group["clientName"]),
                            ("tax year", group["taxYear"]),
                            ("return type", group["returnType"]),
                        )
                        if value is None
                    ]

                    if missing:
                        public["issues"].append(
                            "Assign this group explicitly: source does not establish "
                            + ", ".join(missing)
                            + "."
                        )
                    else:
                        try:
                            with _savepoint(connection):
                                detail = self._assign_group(
                                    connection, document, group, pages, content, media_type
                                )
                            public.update(
                                status="assigned",
                                returnId=detail["id"],
                                clientId=detail["clientId"],
                            )
                        except ReviewError as error:
                            public["issues"].append(error.detail)
                    if not group["values"]:
                        public["issues"].append(
                            "No supported intake values were found in this source. Review the original evidence."
                        )
                    document["groups"].append(public)

                self._refresh_status(document)
                self._save(connection, document)
                connection.commit()
            with self._lock:
                self._pending_returns.update(
                    group["returnId"] for group in document["groups"] if group["returnId"]
                )
        except ReviewError as error:
            self._fail(document_id, error.detail)
        except Exception:
            self._fail(
                document_id,
                "Extraction could not be completed. No unverified values were imported. Retry or assign the document manually after checking the source.",
            )
        finally:
            with self._lock:
                self._active.discard(document_id)
            self._prepare_pending()

    def _prepare_pending(self) -> None:
        with self._lock:
            if self._active or self._closed:
                return
            return_ids = sorted(self._pending_returns)
            self._pending_returns.clear()
        if self.prepare_return is None:
            return

        for return_id in return_ids:
            try:
                self.prepare_return(return_id)
            except Exception as error:
                message = error.detail if isinstance(error, ReviewError) else "Automatic form preparation could not be queued."
                with self._lock, self.store._connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    for row in connection.execute("SELECT payload FROM review_intake_documents").fetchall():
                        document = json.loads(row["payload"])
                        for group in document["groups"]:
                            if group.get("returnId") == return_id:
                                group["issues"].append(message)
                                self._save(connection, document)
                    connection.commit()

    @staticmethod
    def _refresh_status(document: dict) -> None:
        document.update(
            status="completed"
            if all(group["status"] == "assigned" for group in document["groups"])
            else "needs_assignment",
            error=None,
            updatedAt=_now(),
        )

    def _fail(self, document_id: str, error: str) -> None:
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _, document = self._get(connection, document_id)
            document.update(status="failed", error=error, updatedAt=_now())
            self._save(connection, document)
            connection.commit()

    def _assign_group(
        self,
        connection,
        document: dict,
        group: dict,
        pages: list[dict],
        content: bytes,
        media_type: str,
        *,
        return_id: str | None = None,
        client_name: str | None = None,
        tax_year: int | None = None,
        return_type: str | None = None,
    ) -> dict:
        year = tax_year if tax_year is not None else group["taxYear"]
        kind = return_type if return_type is not None else group["returnType"]
        name = client_name if client_name is not None else group["clientName"]
        target = self.store._load(connection, return_id) if return_id is not None else None
        if target is not None:
            year = target["taxYear"] if year is None else year
            kind = target["returnType"] if kind is None else kind
        from datetime import date

        if type(year) is not int or not 1990 <= year <= date.today().year:
            raise ReviewError(
                422,
                f"Choose a tax year from 1990 through {date.today().year} for this source group.",
            )
        if kind not in {"individual", "business"}:
            raise ReviewError(422, "Choose individual or business for this source group.")
        # A known source year/type cannot silently be assigned to a different workspace.
        if group["taxYear"] is not None and year != group["taxYear"]:
            raise ReviewError(422, "The selected tax year differs from the verified source year.")
        if group["returnType"] is not None and kind != group["returnType"]:
            raise ReviewError(
                422, "The selected return type differs from the extracted source group."
            )
        identity_hash = group["identityHash"]
        identity = (
            connection.execute(
                "SELECT client_id, return_type FROM review_intake_identities WHERE identity_hash = ?",
                (identity_hash,),
            ).fetchone()
            if identity_hash
            else None
        )
        if identity is not None and identity["return_type"] != kind:
            raise ReviewError(
                422,
                "The matching client has a different return type. Choose the intended profile.",
            )
        if return_id is not None:
            detail = target
            if detail["taxYear"] != year or detail["returnType"] != kind:
                raise ReviewError(422, "Choose a return matching this group's tax year and type.")
            if identity is not None and identity["client_id"] != detail["clientId"]:
                raise ReviewError(
                    422,
                    "These contact details belong to another client. Choose that client's matching return.",
                )
            self.ensure_idle(detail["id"])
        else:
            saved = [
                json.loads(row["payload"])
                for row in connection.execute("SELECT payload FROM review_returns ORDER BY rowid")
            ]
            matches = [
                item
                for item in saved
                if identity is not None and item["clientId"] == identity["client_id"]
            ]
            incoming = {item["fieldId"]: item["value"] for item in group["values"]}
            if identity is not None:
                for item in matches:
                    previous = {field["id"]: field["value"] for field in item["fields"]}
                    if (
                        incoming.get("date_of_birth") and previous.get("date_of_birth")
                        and incoming["date_of_birth"] != previous["date_of_birth"]
                    ):
                        raise ReviewError(422, "The matching client's birth date conflicts. Choose the intended profile.")
            elif isinstance(name, str):
                named = [
                    item for item in saved
                    if item["returnType"] == kind and _space(item["clientName"]) == _space(name)
                ]
                matches = []
                conflicting_clients = set()
                for item in named:
                    previous = {field["id"]: field["value"] for field in item["fields"]}
                    birth = incoming.get("date_of_birth")
                    previous_birth = previous.get("date_of_birth")
                    email = incoming.get("email")
                    previous_email = previous.get("email")
                    birth_conflict = birth and previous_birth and birth != previous_birth
                    email_conflict = email and previous_email and email.casefold() != previous_email.casefold()
                    if birth_conflict or email_conflict:
                        conflicting_clients.add(item["clientId"])
                    else:
                        matches.append(item)
                matches = [item for item in matches if item["clientId"] not in conflicting_clients]
                if len({item["clientId"] for item in matches}) > 1:
                    raise ReviewError(422, "Several clients match this name. Choose the correct profile or create a separate one.")
                if named and not matches:
                    raise ReviewError(422, "The matching name has conflicting contact details. Choose the correct profile or create a separate one.")
            existing = next((item for item in matches if item["taxYear"] == year), None)
            if existing is not None:
                detail = self.store._load(connection, existing["id"])
                self.ensure_idle(detail["id"])
            else:
                if not isinstance(name, str) or not name.strip() or len(name) > 200:
                    raise ReviewError(
                        422, "Enter a client name before creating a workspace for this group."
                    )
                client_id = matches[0]["clientId"] if matches else (
                    identity["client_id"] if identity is not None else f"client-{uuid.uuid4().hex}"
                )
                if matches:
                    name = matches[0]["clientName"]
                detail = blank_return(
                    f"return-{uuid.uuid4().hex}", client_id, name.strip(), year, kind
                )
                connection.execute(
                    "INSERT INTO review_returns (id, payload, updated_at) VALUES (?, ?, ?)",
                    (detail["id"], json.dumps(detail), _now()),
                )
        if identity_hash and identity is None:
            # An explicit human assignment can teach stable identity, never a name-only match.
            connection.execute(
                "INSERT INTO review_intake_identities (identity_hash, client_id, return_type) VALUES (?, ?, ?)",
                (identity_hash, detail["clientId"], kind),
            )
        return self.store.import_extracted_group(
            connection,
            detail,
            filename=document["filename"],
            content=content,
            media_type=media_type,
            values=group["values"],
            pages=pages,
        )

    def assign(
        self,
        document_id: str,
        group_id: str,
        return_id: str | None = None,
        client_name: str | None = None,
        tax_year: int | None = None,
        return_type: str | None = None,
    ) -> dict:
        with self._lock, self.mutation_lock, self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, document = self._get(connection, document_id)
            if document["status"] == "processing":
                raise ReviewError(
                    409, "Wait for document extraction to finish before assigning it."
                )
            group = next(
                (item for item in document.get("_extractedGroups", []) if item["id"] == group_id),
                None,
            )
            public = next((item for item in document["groups"] if item["id"] == group_id), None)
            if group is None or public is None:
                raise ReviewError(404, "Extracted source group not found.")
            if public["status"] == "assigned":
                if return_id is None or public["returnId"] == return_id:
                    return self._public(document)
                raise ReviewError(
                    409,
                    "This source group is already assigned. Its evidence was preserved in the original return.",
                )
            content = bytes(row["content"])
            pages = source_pages(content, row["media_type"])
            detail = self._assign_group(
                connection,
                document,
                group,
                pages,
                content,
                row["media_type"],
                return_id=return_id,
                client_name=client_name,
                tax_year=tax_year,
                return_type=return_type,
            )
            public.update(
                status="assigned",
                returnId=detail["id"],
                clientId=detail["clientId"],
                issues=list(group.get("issues", [])) + (
                    [] if group["values"] else [
                        "No supported intake values were found in this source. Review the original evidence."
                    ]
                ),
                clientName=detail["clientName"],
                taxYear=detail["taxYear"],
                returnType=detail["returnType"],
            )
            self._refresh_status(document)
            self._save(connection, document)
            connection.commit()
        with self._lock:
            self._pending_returns.add(detail["id"])
        self._prepare_pending()
        with self.store._connection() as connection:
            return self._public(self._get(connection, document_id)[1])

    def close(self) -> None:
        with self._lock:
            self._closed = True
        # Each external request has a bounded timeout and no retries. Let running work commit safely.
        self._executor.shutdown(wait=True, cancel_futures=True)
        for document_id in list(self._active):
            self._fail(
                document_id,
                "Extraction stopped during service shutdown. Approve the document again to retry.",
            )
        self._active.clear()


class _savepoint:
    """One rejected group must not roll back other clients' valid groups."""

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.execute("SAVEPOINT intake_group")

    def __exit__(self, kind, value, traceback):
        if kind is not None:
            self.connection.execute("ROLLBACK TO intake_group")
        self.connection.execute("RELEASE intake_group")
        return False
