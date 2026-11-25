"""Committed batch intake for the reference coordinator.

The service supplies an authenticated ingest scope and a protected, immutable
object store. No endpoint claims a return assignment or decides data class from
an upload hint. A drop tracks upload progress; each committed file can proceed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from psycopg import Connection
from psycopg.rows import DictRow
from pydantic import Field, StrictBool, StrictInt, StringConstraints

from ..review_documents import MAX_PDF_BYTES, DocumentError, source_pages
from ..review_store import ReviewError, _document_title
from .contracts import Contract, Text
from .coordinator_db import CoordinatorDB
from .dispatch import ExtractionGrant, ExtractionPolicy, ExtractionTicket, UploadCommitted, VerifiedUpload
from .tool_contract import Ref

MediaType = Literal["application/pdf", "text/plain"]

ClientName = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=160)]
TaxYear = Annotated[StrictInt, Field(ge=1900, le=9999)]


class NewClient(Contract):
    name: ClientName


class IntakeClient(NewClient):
    client_id: Ref
    tax_years: tuple[TaxYear, ...] | None = None


class ClientAssignment(Contract):
    client_id: Ref
    tax_year: TaxYear


class BatchFile(Contract):
    slot: Ref
    filename: Text
    media_type: MediaType


class BatchManifest(Contract):
    batch_id: Ref
    files: Annotated[tuple[BatchFile, ...], Field(min_length=1, max_length=100)]
    approved_for_ai: StrictBool = Field(alias="approvedForAI")
    return_hint: Ref | None = None
    assignment: ClientAssignment | None = None


class InboxFact(Contract):
    label: str
    value: str
    page: int | None
    line: int
    quote: str
    rect: tuple[float, float, float, float] | None = None


class InboxAnalysis(Contract):
    status: Literal["pending", "filed", "needs_review", "failed"]
    category: str | None = None
    forms: tuple[str, ...] = ()
    facts: tuple[InboxFact, ...] = ()
    message: str | None = None


class InboxPractice(Contract):
    status: Literal["waiting", "queued", "preparing", "ready", "needs_review", "failed"]
    message: str | None = None
    gated: str | None = None
    url: str | None = None
    bindings: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)


class BatchSlotView(Contract):
    slot: Ref
    filename: str
    media_type: MediaType
    state: Literal["pending", "uploaded", "excluded"]
    upload_id: Ref | None
    version_id: Ref | None
    analysis: InboxAnalysis | None = None
    practice: InboxPractice | None = None
    assignment: ClientAssignment | None = None


class BatchView(Contract):
    batch_id: Ref
    state: Literal["incomplete", "sealed", "excluded"]
    approved_for_ai: bool = Field(alias="approvedForAI")
    files: tuple[BatchSlotView, ...]


@dataclass(frozen=True, slots=True)
class IngestScope:
    """Trusted authorization context supplied by the firm's service, not the POST body."""

    firm_id: str
    actor_id: str
    # Only explicit, already authorized return hints are accepted from this actor.
    return_ids: frozenset[str]


class ImmutableObjects(Protocol):
    def put_if_absent(self, firm_id: str, digest: str, content: bytes) -> str:
        """Store immutable bytes under the firm's scope and return an opaque object key."""
        ...

    def read(self, firm_id: str, object_id: str) -> bytes | None: ...

    def checksum(self, firm_id: str, object_id: str) -> str | None:
        """Return protected storage's verified digest, not client-supplied metadata."""
        ...


@dataclass(frozen=True, slots=True)
class ClassifiedUpload:
    """An independent finding, never populated solely from client fields or consent."""

    return_id: str | None = None
    data_class: Literal["synthetic", "approved_redacted"] | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS prep_upload_batches (
    firm_id text NOT NULL,
    actor_id text NOT NULL,
    batch_id text NOT NULL,
    approved boolean NOT NULL,
    return_hint text,
    consent_grant_id text,
    policy_provider text,
    policy_account text,
    policy_model text,
    policy_endpoint text,
    policy_class text,
    consent_expires_at timestamptz,
    sealed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (firm_id, actor_id, batch_id)
);
CREATE TABLE IF NOT EXISTS prep_local_clients (
    client_id text PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE IF NOT EXISTS prep_upload_slots (
    firm_id text NOT NULL,
    actor_id text NOT NULL,
    batch_id text NOT NULL,
    slot text NOT NULL,
    filename text NOT NULL,
    media_type text NOT NULL,
    excluded boolean NOT NULL DEFAULT false,
    upload_id text,
    version_id text,
    object_id text,
    digest text,
    PRIMARY KEY (firm_id, actor_id, batch_id, slot),
    UNIQUE (firm_id, upload_id, version_id),
    FOREIGN KEY (firm_id, actor_id, batch_id) REFERENCES prep_upload_batches,
    CHECK ((upload_id IS NULL AND version_id IS NULL AND object_id IS NULL AND digest IS NULL) OR
           (NOT excluded AND upload_id IS NOT NULL AND version_id IS NOT NULL AND object_id IS NOT NULL AND digest IS NOT NULL))
);
ALTER TABLE prep_upload_slots ADD COLUMN IF NOT EXISTS assigned_client_id text;
ALTER TABLE prep_upload_slots ADD COLUMN IF NOT EXISTS assigned_tax_year integer
    CHECK ((assigned_client_id IS NULL AND assigned_tax_year IS NULL) OR
           (assigned_client_id IS NOT NULL AND assigned_tax_year IS NOT NULL AND assigned_tax_year BETWEEN 1900 AND 9999));
CREATE TABLE IF NOT EXISTS prep_upload_outbox (
    firm_id text NOT NULL,
    upload_id text NOT NULL,
    version_id text NOT NULL,
    published_at timestamptz,
    attempted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (firm_id, upload_id, version_id),
    FOREIGN KEY (firm_id, upload_id, version_id) REFERENCES prep_upload_slots (firm_id, upload_id, version_id)
);
CREATE INDEX IF NOT EXISTS prep_upload_outbox_pending ON prep_upload_outbox (attempted_at NULLS FIRST, created_at)
    WHERE published_at IS NULL;
"""


def install_batch_schema(conn: Connection[DictRow]) -> None:
    conn.execute(_SCHEMA)


def _batch_key(scope: IngestScope, batch_id: str) -> tuple[str, str, str]:
    return scope.firm_id, scope.actor_id, batch_id


class BatchIntake:
    def __init__(self, db: CoordinatorDB, objects: ImmutableObjects, *, require_assignment: bool = False) -> None:
        self._db = db
        self._objects = objects
        self._require_assignment = require_assignment

    def install(self) -> None:
        with self._db.transaction() as conn:
            install_batch_schema(conn)

    def local_clients(self) -> tuple[IntakeClient, ...]:
        with self._db.transaction() as conn:
            rows = conn.execute("SELECT client_id, name FROM prep_local_clients ORDER BY lower(name), client_id").fetchall()
        return tuple(IntakeClient.model_validate(row) for row in rows)

    def create_local_client(self, client: NewClient) -> IntakeClient:
        stored = IntakeClient(client_id=uuid4().hex, name=client.name)
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO prep_local_clients (client_id, name) VALUES (%s, %s)",
                         (stored.client_id, stored.name))
        return stored

    def require_local_client(self, assignment: ClientAssignment) -> None:
        with self._db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM prep_local_clients WHERE client_id=%s", (assignment.client_id,)).fetchone():
                raise ReviewError(404, "Client not found.")

    def assign_slot(
        self, scope: IngestScope, batch_id: str, slot_id: str, assignment: ClientAssignment,
    ) -> BatchView:
        key = _batch_key(scope, batch_id)
        with self._db.transaction() as conn:
            batch = self._batch(conn, key, lock=True)
            slot = self._slot(conn, key, slot_id, return_id=None)
            if slot["excluded"]:
                raise ReviewError(409, "An excluded file cannot be assigned.")
            current = _assignment(slot)
            if current is not None and current != assignment:
                raise ReviewError(409, "This file already belongs to a client and tax year.")
            if current is None:
                conn.execute(
                    """UPDATE prep_upload_slots SET assigned_client_id=%s, assigned_tax_year=%s
                       WHERE (firm_id, actor_id, batch_id, slot)=(%s,%s,%s,%s)""",
                    (assignment.client_id, assignment.tax_year, *key, slot_id),
                )
            return self._view(batch, self._slots(conn, key))

    def declare(
        self, scope: IngestScope, manifest: BatchManifest, *, policy: ExtractionPolicy | None = None,
        consent_valid_for: timedelta = timedelta(hours=1),
    ) -> BatchView:
        assignment = manifest.assignment
        if self._require_assignment and assignment is None:
            raise ReviewError(422, "Choose a client and tax year before uploading.")
        if manifest.return_hint is not None and manifest.return_hint not in scope.return_ids:
            raise ReviewError(404, "Return not found in the upload scope.")
        files = {file.slot: (_document_title(file.filename), file.media_type) for file in manifest.files}
        if len(files) != len(manifest.files):
            raise ReviewError(422, "A batch must declare unique file slots.")
        if not manifest.approved_for_ai:
            policy = None
        if policy is not None and (
            not timedelta(0) < consent_valid_for <= timedelta(days=1)
            or policy.data_class not in {"synthetic", "approved_redacted"}
        ):
            raise ValueError("Invalid approved extraction policy for intake.")

        key = _batch_key(scope, manifest.batch_id)
        with self._db.transaction() as conn:
            added = conn.execute(
                """INSERT INTO prep_upload_batches
                   (firm_id, actor_id, batch_id, approved, return_hint, consent_grant_id,
                    policy_provider, policy_account, policy_model, policy_endpoint, policy_class, consent_expires_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp() + %s::interval)
                   ON CONFLICT DO NOTHING""",
                (*key, manifest.approved_for_ai, manifest.return_hint,
                 uuid4().hex if policy else None, policy.provider_id if policy else None,
                 policy.account_id if policy else None, policy.model_id if policy else None,
                 policy.endpoint if policy else None, policy.data_class if policy else None,
                 consent_valid_for if policy else None),
            ).rowcount
            batch = self._batch(conn, key, lock=True)
            if added:
                with conn.cursor() as cursor:
                    cursor.executemany(
                        """INSERT INTO prep_upload_slots
                           (firm_id, actor_id, batch_id, slot, filename, media_type, assigned_client_id, assigned_tax_year)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        ((*key, slot, filename, media_type, assignment.client_id if assignment else None,
                          assignment.tax_year if assignment else None) for slot, (filename, media_type) in files.items()),
                    )
            rows = self._slots(conn, key)
            if (
                files != {row["slot"]: (row["filename"], row["media_type"]) for row in rows}
                or batch["approved"] != manifest.approved_for_ai or batch["return_hint"] != manifest.return_hint
                or (assignment is not None and any(_assignment(row) != assignment for row in rows))
            ):
                raise ReviewError(409, "This batch ID already has a different manifest.")
            return self._view(batch, rows)

    def status(self, scope: IngestScope, batch_id: str) -> BatchView:
        key = _batch_key(scope, batch_id)
        with self._db.transaction() as conn:
            return self._view(self._batch(conn, key, lock=True), self._slots(conn, key))

    def original(self, scope: IngestScope, batch_id: str, slot_id: str) -> tuple[MediaType, bytes]:
        with self._db.transaction() as conn:
            slot = self._slot(conn, _batch_key(scope, batch_id), slot_id, return_id=None)
        if slot["object_id"] is None:
            raise ReviewError(404, "No original was committed for this slot.")
        content = self._objects.read(scope.firm_id, slot["object_id"])
        if content is None:
            raise ReviewError(404, "The committed original is unavailable.")
        return slot["media_type"], content

    def list_batches(self, scope: IngestScope, *, return_id: str | None = None) -> tuple[BatchView, ...]:
        """A return sees only its independently attributed committed files, not pending drop controls."""
        self._require_return(scope, return_id)
        with self._db.transaction() as conn:
            if return_id is None:
                rows = conn.execute(
                    """SELECT * FROM prep_upload_batches WHERE (firm_id, actor_id) = (%s, %s)
                       ORDER BY created_at DESC, batch_id FOR SHARE""",
                    (scope.firm_id, scope.actor_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT b.* FROM prep_upload_batches b WHERE (b.firm_id,b.actor_id)=(%s,%s)
                         AND EXISTS (
                           SELECT 1 FROM prep_upload_slots s
                           JOIN prep_dispatch_uploads d USING (firm_id,upload_id,version_id)
                           WHERE (s.firm_id,s.actor_id,s.batch_id)=(b.firm_id,b.actor_id,b.batch_id)
                             AND d.return_id=%s AND d.state <> 'misassigned'
                         )
                       ORDER BY b.created_at DESC, b.batch_id LIMIT 100 FOR SHARE OF b""",
                    (scope.firm_id, scope.actor_id, return_id),
                ).fetchall()
            views = []
            for batch in rows:
                files = self._slots(conn, _batch_key(scope, batch["batch_id"]), return_id=return_id)
                if files:
                    views.append(self._view(batch, files, committed_only=return_id is not None))
            return tuple(views)

    def put(
        self, scope: IngestScope, batch_id: str, slot_id: str, media_type: str, content: bytes,
        *, return_id: str | None = None, require_assignment: bool = False,
    ) -> BatchView:
        self._require_return(scope, return_id)
        if len(content) > MAX_PDF_BYTES:
            raise ReviewError(413, "Documents must be at most 10 MiB.")
        key = _batch_key(scope, batch_id)
        digest = sha256(content).hexdigest()
        with self._db.transaction() as conn:
            batch = self._batch(conn, key, lock=True)
            slot = self._slot(conn, key, slot_id, return_id=return_id)
            if (self._require_assignment or require_assignment) and _assignment(slot) is None:
                raise ReviewError(422, "Assign this file to a client and tax year before uploading.")
            if self._check_put(batch, slot, media_type, digest):
                return self._view(
                    batch, self._slots(conn, key, return_id=return_id), committed_only=return_id is not None,
                )

        # Parsing and protected storage can be slow. Neither holds the batch row lock;
        # a simultaneous completion or exclusion is checked again before any version is committed.
        try:
            source_pages(content, media_type)
        except DocumentError as error:
            raise ReviewError(error.status_code, error.detail) from None
        # An interrupted writer may leave an unreferenced immutable object, never an event missing its bytes.
        object_id = self._objects.put_if_absent(scope.firm_id, digest, content)
        if not object_id:
            raise ReviewError(503, "Protected storage did not acknowledge the original.")

        with self._db.transaction() as conn:
            batch = self._batch(conn, key, lock=True)
            slot = self._slot(conn, key, slot_id, return_id=return_id)
            if self._check_put(batch, slot, media_type, digest):
                return self._view(
                    batch, self._slots(conn, key, return_id=return_id), committed_only=return_id is not None,
                )
            upload_id, version_id = uuid4().hex, uuid4().hex
            conn.execute(
                """UPDATE prep_upload_slots SET upload_id=%s,version_id=%s,object_id=%s,digest=%s
                   WHERE (firm_id, actor_id, batch_id, slot) = (%s,%s,%s,%s)""",
                (upload_id, version_id, object_id, digest, *key, slot_id),
            )
            conn.execute(
                "INSERT INTO prep_upload_outbox (firm_id, upload_id, version_id) VALUES (%s,%s,%s)",
                (scope.firm_id, upload_id, version_id),
            )
            return self._seal(conn, key, batch)

    def exclude(self, scope: IngestScope, batch_id: str, slot_id: str, *, return_id: str | None = None) -> BatchView:
        self._require_return(scope, return_id)
        key = _batch_key(scope, batch_id)
        with self._db.transaction() as conn:
            batch = self._batch(conn, key, lock=True)
            slot = self._slot(conn, key, slot_id, return_id=return_id)
            if slot["excluded"]:
                return self._view(batch, self._slots(conn, key))
            if batch["sealed_at"] or slot["upload_id"]:
                raise ReviewError(409, "Only an incomplete slot can be excluded.")
            conn.execute(
                "UPDATE prep_upload_slots SET excluded=true WHERE (firm_id, actor_id, batch_id, slot) = (%s,%s,%s,%s)",
                (*key, slot_id),
            )
            return self._seal(conn, key, batch)

    def committed_events(self, *, firm_id: str | None = None, limit: int = 100) -> tuple[UploadCommitted, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("Outbox page limit must be between 1 and 1000.")
        with self._db.transaction() as conn:
            rows = conn.execute(
                """SELECT firm_id,upload_id,version_id FROM prep_upload_outbox
                   WHERE published_at IS NULL AND (%s::text IS NULL OR firm_id=%s)
                   ORDER BY attempted_at NULLS FIRST, created_at, upload_id LIMIT %s""", (firm_id, firm_id, limit),
            ).fetchall()
        return tuple(UploadCommitted(**row) for row in rows)

    def record_publication(self, event: UploadCommitted, *, received: bool) -> None:
        """Persist attempts separately; only an acknowledged durable receiver retires an event."""
        with self._db.transaction() as conn:
            conn.execute(
                """UPDATE prep_upload_outbox SET attempted_at=clock_timestamp(),
                   published_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END
                   WHERE (firm_id,upload_id,version_id)=(%s,%s,%s) AND published_at IS NULL""",
                (received, event.firm_id, event.upload_id, event.version_id),
            )

    def _committed(self, event: UploadCommitted) -> tuple[DictRow, DictRow] | None:
        with self._db.transaction() as conn:
            slot = conn.execute(
                """SELECT s.* FROM prep_upload_slots s
                   JOIN prep_upload_outbox o USING (firm_id,upload_id,version_id)
                   WHERE (o.firm_id,o.upload_id,o.version_id)=(%s,%s,%s)""",
                (event.firm_id, event.upload_id, event.version_id),
            ).fetchone()
            if slot is None:
                return None
            key = slot["firm_id"], slot["actor_id"], slot["batch_id"]
            return self._batch(conn, key), slot

    @staticmethod
    def _require_return(scope: IngestScope, return_id: str | None) -> None:
        if return_id is not None and return_id not in scope.return_ids:
            raise ReviewError(404, "Return not found in the upload scope.")

    @staticmethod
    def _check_put(batch: DictRow, slot: DictRow, media_type: str, digest: str) -> bool:
        if media_type != slot["media_type"]:
            raise ReviewError(415, "The content type does not match the declared file.")
        if slot["digest"] == digest:
            return True
        if batch["sealed_at"] or slot["excluded"] or slot["digest"]:
            raise ReviewError(409, "The declared slot can no longer be changed.")
        return False

    @staticmethod
    def _batch(conn: Connection[DictRow], key: Sequence[str], *, lock: bool = False) -> DictRow:
        query = "SELECT * FROM prep_upload_batches WHERE (firm_id, actor_id, batch_id)=(%s,%s,%s)"
        row = conn.execute(query + (" FOR UPDATE" if lock else ""), key).fetchone()
        if row is None:
            raise ReviewError(404, "Upload batch not found in this scope.")
        return row

    @staticmethod
    def _slot(conn: Connection[DictRow], key: Sequence[str], slot_id: str, *, return_id: str | None) -> DictRow:
        rows = BatchIntake._slots(conn, key, return_id=return_id, slot_id=slot_id)
        if not rows:
            raise ReviewError(404, "File slot not found in this upload scope.")
        return rows[0]

    @staticmethod
    def _slots(
        conn: Connection[DictRow], key: Sequence[str], *, return_id: str | None = None, slot_id: str | None = None,
    ) -> list[DictRow]:
        query = "SELECT s.* FROM prep_upload_slots s"
        conditions = ["(s.firm_id,s.actor_id,s.batch_id)=(%s,%s,%s)"]
        params = list(key)
        if return_id is not None:
            query += " JOIN prep_dispatch_uploads d USING (firm_id,upload_id,version_id)"
            conditions.append("d.return_id=%s AND d.state <> 'misassigned'")
            params.append(return_id)
        if slot_id is not None:
            conditions.append("s.slot=%s")
            params.append(slot_id)
        return conn.execute(query + " WHERE " + " AND ".join(conditions) + " ORDER BY s.slot", params).fetchall()

    def _seal(self, conn: Connection[DictRow], key: Sequence[str], batch: DictRow) -> BatchView:
        slots = self._slots(conn, key)
        if not any(not row["excluded"] and not row["upload_id"] for row in slots):
            conn.execute(
                "UPDATE prep_upload_batches SET sealed_at=clock_timestamp() WHERE (firm_id, actor_id, batch_id)=(%s,%s,%s)",
                key,
            )
            batch = self._batch(conn, key)
        return self._view(batch, slots)

    @staticmethod
    def _view(batch: DictRow, rows: list[DictRow], *, committed_only: bool = False) -> BatchView:
        if committed_only:
            state = "sealed"
        elif not batch["sealed_at"]:
            state = "incomplete"
        elif all(row["excluded"] for row in rows):
            state = "excluded"
        else:
            state = "sealed"
        files = tuple(BatchSlotView(
            slot=row["slot"], filename=row["filename"], media_type=row["media_type"],
            state="excluded" if row["excluded"] else "uploaded" if row["upload_id"] else "pending",
            upload_id=row["upload_id"], version_id=row["version_id"],
            assignment=_assignment(row),
        ) for row in rows)
        return BatchView(batch_id=batch["batch_id"], state=state, approved_for_ai=batch["approved"], files=files)


def _assignment(slot: DictRow) -> ClientAssignment | None:
    if slot["assigned_client_id"] is None:
        return None
    return ClientAssignment(client_id=slot["assigned_client_id"], tax_year=slot["assigned_tax_year"])


class CommittedBatchSource:
    """Trusted receipt verification adapter for the reference upload writer."""

    def __init__(
        self, intake: BatchIntake,
        classify: Callable[[UploadCommitted, str | None], ClassifiedUpload],
    ) -> None:
        self._intake = intake
        self._classify = classify

    def verify(self, event: UploadCommitted) -> VerifiedUpload | None:
        result = self._intake._committed(event)
        return self._verify(event, *result) if result else None

    def _verify(self, event: UploadCommitted, batch: DictRow, slot: DictRow) -> VerifiedUpload | None:
        if self._intake._objects.checksum(event.firm_id, slot["object_id"]) != slot["digest"]:
            return None
        classification = self._classify(event, batch["return_hint"])
        return self._receipt(event, batch, slot, classification)

    @staticmethod
    def _receipt(
        event: UploadCommitted, batch: DictRow, slot: DictRow, classification: ClassifiedUpload,
    ) -> VerifiedUpload:
        # Only the exact committed slot is covered; no siblings or return hint are treated as authority.
        scope = sha256(json.dumps([batch["actor_id"], batch["batch_id"]]).encode()).hexdigest()
        return VerifiedUpload(event, scope, slot["object_id"], slot["digest"],
                              classification.return_id, classification.data_class)

    def grant_id(self, event: UploadCommitted) -> str | None:
        result = self._intake._committed(event)
        return result[0]["consent_grant_id"] if result else None

    def read_extraction(self, ticket: ExtractionTicket) -> tuple[MediaType, bytes]:
        """Read only the version covered by an already issued extraction ticket."""
        if ticket.grant.event != ticket.event:
            raise ReviewError(403, "The extraction ticket does not cover this original.")
        result = self._intake._committed(ticket.event)
        if result is None:
            raise ReviewError(404, "The committed original is unavailable.")
        _, slot = result
        if (slot["object_id"], slot["digest"]) != (ticket.object_id, ticket.sha256):
            raise ReviewError(409, "The original does not match the authorized version.")
        content = self._intake._objects.read(ticket.event.firm_id, ticket.object_id)
        if content is None or sha256(content).hexdigest() != ticket.sha256:
            raise ReviewError(409, "The original does not match the authorized version.")
        return slot["media_type"], content

    def verify_grant(self, event: UploadCommitted, grant_id: str) -> ExtractionGrant | None:
        result = self._intake._committed(event)
        if result is None:
            return None
        batch, slot = result
        if (
            not batch["approved"] or batch["consent_grant_id"] != grant_id
            or batch["consent_expires_at"] is None
        ):
            return None
        upload = self._receipt(event, batch, slot, ClassifiedUpload())
        return ExtractionGrant(grant_id, event, upload.receipt_id, ExtractionPolicy(
            batch["policy_provider"], batch["policy_account"], batch["policy_model"],
            batch["policy_class"], batch["policy_endpoint"],
        ), batch["consent_expires_at"])
