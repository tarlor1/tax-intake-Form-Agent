"""Receive committed upload versions, freeze scoped inputs, and relay job IDs."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from .coordinator_db import CoordinatorDB

if TYPE_CHECKING:
    from .agent import StagePolicy


@dataclass(frozen=True)
class UploadCommitted:
    firm_id: str
    upload_id: str
    version_id: str


@dataclass(frozen=True)
class VerifiedUpload:
    """One committed immutable original and its declaration or portal authorization scope."""

    event: UploadCommitted
    authorization_scope: str
    object_id: str
    sha256: str
    return_id: str | None = None
    data_class: str | None = None

    def __post_init__(self) -> None:
        if (
            not all((*_identity(self.event), self.authorization_scope, self.object_id))
            or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None
        ):
            raise ValueError("A verified upload must identify a committed, immutable original")

    @property
    def receipt_id(self) -> str:
        return _digest([*_identity(self.event), self.authorization_scope, self.object_id, self.sha256])


@dataclass(frozen=True)
class ExtractionPolicy:
    provider_id: str
    account_id: str
    model_id: str
    data_class: str
    endpoint: str


@dataclass(frozen=True)
class ExtractionGrant:
    grant_id: str
    event: UploadCommitted
    receipt_id: str
    policy: ExtractionPolicy
    expires_at: datetime


class UploadSource(Protocol):
    def verify(self, event: UploadCommitted) -> VerifiedUpload | None:
        """Check the durable feed and immutable original for this upload only.

        Return None before the upload commits. Do not infer assignment from a hint.
        """
        ...

    def verify_grant(self, event: UploadCommitted, grant_id: str) -> ExtractionGrant | None:
        """Resolve an actual authorization reference from the trusted issuer, not message claims."""
        ...


@dataclass(frozen=True)
class ExtractionTicket:
    event: UploadCommitted
    attempt_id: str
    object_id: str
    sha256: str
    grant: ExtractionGrant
    return_id: str | None


@dataclass(frozen=True)
class PreparedExtraction:
    """An independent assignment plus a transaction-bound write of already-grounded facts.

    The write is never called for an unassigned, stale or misassigned result. It
    must use the supplied connection exclusively and return the durable artifact ID.
    """

    return_id: str | None
    persist: Callable[[Connection[DictRow]], str] | None = None

    def __post_init__(self) -> None:
        if (self.return_id is None) != (self.persist is None) or self.return_id == "":
            raise ValueError("An assigned extraction requires a scoped artifact writer")


ExtractionClaim = ExtractionTicket | Literal["denied", "no-work"]
Disclosure = Literal["authorized", "denied", "no-work"]


class _ExpiredAttempt(Exception):
    """Roll back a staged artifact that did not finish within its durable fence."""


@dataclass(frozen=True)
class QueuedJob:
    firm_id: str
    job_id: str


class JobPublisher(Protocol):
    def publish(self, job: QueuedJob, *, idempotency_key: str) -> None:
        """Return on durable broker acknowledgement; repeated keys must represent one message."""
        ...


@dataclass(frozen=True)
class PublicationResult:
    attempted: int
    published: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS prep_dispatch_uploads (
    firm_id text NOT NULL,
    receipt_id text NOT NULL,
    upload_id text NOT NULL,
    version_id text NOT NULL,
    object_id text NOT NULL,
    sha256 text NOT NULL,
    data_class text,
    return_id text,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN (
        'pending', 'extracting', 'accepted', 'unassigned',
        'misassigned', 'no_authorization', 'interrupted'
    )),
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    attempt_id text,
    grant_id text,
    extraction_policy jsonb,
    extraction_started_at timestamptz,
    grant_expires_at timestamptz,
    extraction_deadline timestamptz,
    disclosure_started_at timestamptz,
    staged_state text CHECK (staged_state IN ('accepted', 'unassigned', 'misassigned')),
    settled_at timestamptz,
    artifact_id text,
    job_id text,
    PRIMARY KEY (firm_id, upload_id, version_id),
    FOREIGN KEY (firm_id, job_id) REFERENCES prep_jobs
);
CREATE INDEX IF NOT EXISTS prep_dispatch_uploads_return
    ON prep_dispatch_uploads (firm_id, return_id) WHERE job_id IS NULL;
CREATE INDEX IF NOT EXISTS prep_dispatch_uploads_pending
    ON prep_dispatch_uploads (received_at, firm_id, upload_id, version_id) WHERE state = 'pending';
CREATE TABLE IF NOT EXISTS prep_job_outbox (
    firm_id text NOT NULL,
    job_id text NOT NULL,
    last_attempt_at timestamptz,
    published_at timestamptz,
    PRIMARY KEY (firm_id, job_id),
    FOREIGN KEY (firm_id, job_id) REFERENCES prep_jobs
);
CREATE INDEX IF NOT EXISTS prep_job_outbox_pending ON prep_job_outbox (last_attempt_at NULLS FIRST, job_id)
    WHERE published_at IS NULL;
"""

DISPATCH_ATTEMPT_UPGRADE_SQL = """
ALTER TABLE prep_dispatch_uploads
    ADD COLUMN IF NOT EXISTS grant_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS extraction_deadline timestamptz,
    ADD COLUMN IF NOT EXISTS disclosure_started_at timestamptz,
    ADD COLUMN IF NOT EXISTS staged_state text CHECK (staged_state IN ('accepted', 'unassigned', 'misassigned'));
CREATE INDEX IF NOT EXISTS prep_dispatch_uploads_recovery
    ON prep_dispatch_uploads (extraction_deadline) WHERE state = 'extracting';
"""


def install_dispatch_schema(conn: Connection[DictRow]) -> None:
    conn.execute(_SCHEMA)
    conn.execute(DISPATCH_ATTEMPT_UPGRADE_SQL)


def _digest(data: object) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _identity(event: UploadCommitted) -> tuple[str, str, str]:
    return event.firm_id, event.upload_id, event.version_id


class UploadDispatch:
    def __init__(
        self, db: CoordinatorDB, source: UploadSource, *, reasoning_policy: StagePolicy | None = None,
    ) -> None:
        self._db = db
        self._source = source
        self._reasoning_policy = None
        if reasoning_policy is not None:
            if reasoning_policy.stage != "reasoning" or not reasoning_policy.document_classes:
                raise ValueError("Dispatch requires a reasoning policy with approved document classes")
            self._reasoning_policy = reasoning_policy.to_wire()

    def install(self) -> None:
        with self._db.transaction() as conn:
            install_dispatch_schema(conn)

    def consume(self, event: UploadCommitted) -> str | None:
        """Record one committed upload and flag its independently attributed return.

        Receipt never depends on other uploads in the same drop. None leaves the
        event replayable. External authorization is checked separately.
        """
        key = _identity(event)
        with self._db.transaction() as conn:
            received = self._upload(conn, event)
            if received is not None:
                return received["receipt_id"]

        verified = self._source.verify(event)
        if verified is None:
            return None
        if verified.event != event:
            raise ValueError("Verified original does not match the upload event")
        receipt_id = verified.receipt_id
        with self._db.transaction() as conn:
            added = conn.execute(
                """INSERT INTO prep_dispatch_uploads
                   (firm_id,upload_id,version_id,receipt_id,object_id,sha256,return_id,data_class)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (*key, receipt_id, verified.object_id, verified.sha256, verified.return_id, verified.data_class),
            ).rowcount
            if not added:
                row = self._upload(conn, event)
                if row["receipt_id"] != receipt_id:
                    raise ValueError("Committed upload receipt changed")
            elif verified.return_id is not None:
                if self._lock_return(conn, event.firm_id, verified.return_id):
                    self._flag(conn, event.firm_id, verified.return_id)
                else:
                    conn.execute(
                        """UPDATE prep_dispatch_uploads SET state='misassigned', settled_at=clock_timestamp()
                           WHERE (firm_id,upload_id,version_id)=(%s,%s,%s)""", key,
                    )
        return receipt_id

    def pending_extractions(self, *, firm_id: str | None = None, limit: int = 100) -> tuple[UploadCommitted, ...]:
        """Recover unclaimed receipts after an upstream event has already been acknowledged."""
        if not 1 <= limit <= 1000:
            raise ValueError("Extraction page limit must be between 1 and 1000")
        with self._db.transaction() as conn:
            rows = conn.execute(
                """SELECT firm_id, upload_id, version_id FROM prep_dispatch_uploads
                   WHERE state='pending' AND (%s::text IS NULL OR firm_id=%s)
                   ORDER BY received_at, firm_id, upload_id, version_id LIMIT %s""",
                (firm_id, firm_id, limit),
            ).fetchall()
        return tuple(UploadCommitted(**row) for row in rows)

    def begin_extraction(
        self, event: UploadCommitted, grant_id: str | None, policy: ExtractionPolicy,
        *, attempt_for: timedelta = timedelta(minutes=5),
    ) -> ExtractionClaim:
        """Claim one durable attempt, distinguishing invalid authority from another worker's policy."""
        if not timedelta(0) < attempt_for <= timedelta(hours=1):
            raise ValueError("Extraction attempt lifetime must be positive and at most one hour")
        if policy.data_class not in {"synthetic", "approved_redacted"}:
            return "no-work"
        current = self._source.verify(event) if grant_id else None
        grant = self._source.verify_grant(event, grant_id) if grant_id else None
        with self._db.transaction() as conn:
            row = self._upload(conn, event, lock=True)
            if row is None or row["state"] != "pending":
                return "no-work"
            denied = self._authorization_failure(conn, event, row, grant_id, current, grant)
            if denied is not None:
                self._finish(conn, event, row, denied)
                return "denied"
            assert grant is not None
            if grant.policy != policy:
                return "no-work"
            attempt_id = uuid4().hex
            updated = conn.execute(
                """UPDATE prep_dispatch_uploads SET state='extracting', attempt_id=%s,
                           grant_id=%s, extraction_policy=%s, extraction_started_at=clock_timestamp(),
                           grant_expires_at=%s, extraction_deadline=LEAST(%s,clock_timestamp()+%s)
                   WHERE (firm_id,upload_id,version_id)=(%s,%s,%s) AND clock_timestamp()<%s""",
                (attempt_id, grant_id, Jsonb(asdict(policy)), grant.expires_at, grant.expires_at,
                 attempt_for, *_identity(event), grant.expires_at),
            ).rowcount
            if not updated:
                self._finish(conn, event, row, "no_authorization")
                return "denied"
            return ExtractionTicket(event, attempt_id, row["object_id"], row["sha256"], grant, row["return_id"])

    def authorize_disclosure(self, ticket: ExtractionTicket, *, continuing: bool = False) -> Disclosure:
        """Recheck the committed original, current assignment/class and exact grant at the provider boundary."""
        event = ticket.event
        current = self._source.verify(event)
        grant = self._source.verify_grant(event, ticket.grant.grant_id)
        with self._db.transaction() as conn:
            row = self._upload(conn, event, lock=True)
            if not self._active_attempt(row, ticket) or (row["disclosure_started_at"] is not None) != continuing:
                return "no-work"
            denied = self._authorization_failure(conn, event, row, ticket.grant.grant_id, current, grant)
            if denied is not None or grant != ticket.grant:
                self._finish(conn, event, row, denied or "no_authorization")
                return "denied"
            if continuing:
                if row["extraction_deadline"] <= self._db_now(conn):
                    self._finish(conn, event, row, "interrupted")
                    return "no-work"
                return "authorized"
            updated = conn.execute(
                """UPDATE prep_dispatch_uploads SET disclosure_started_at=clock_timestamp()
                   WHERE (firm_id,upload_id,version_id)=(%s,%s,%s) AND clock_timestamp()<extraction_deadline""",
                _identity(event),
            ).rowcount
            if not updated:
                self._finish(conn, event, row, "interrupted")
                return "no-work"
            return "authorized"

    def stage_extraction(self, ticket: ExtractionTicket, result: PreparedExtraction) -> bool:
        """Persist an independently grounded artifact and its recoverable link in one transaction."""
        event = ticket.event
        try:
            with self._db.transaction() as conn:
                row = self._upload(conn, event, lock=True)
                if not self._active_attempt(row, ticket) or row["disclosure_started_at"] is None:
                    return False
                previous = row["return_id"]
                assigned = previous if previous is not None else result.return_id
                profile = self._lock_return(conn, event.firm_id, assigned) if assigned is not None else None
                if self._db_now(conn) >= row["extraction_deadline"]:
                    return False
                artifact_id = None
                if (assigned is not None and profile is None) or (
                    previous is not None and result.return_id is not None and previous != result.return_id
                ):
                    state, assigned = "misassigned", previous
                elif result.persist is None:
                    state = "unassigned"
                else:
                    state, artifact_id = "accepted", result.persist(conn)
                    if not artifact_id:
                        raise ValueError("No durable artifact was written for the assigned extraction")
                if previous is None and assigned is not None:
                    self._flag(conn, event.firm_id, assigned)
                updated = conn.execute(
                    """UPDATE prep_dispatch_uploads SET staged_state=%s, return_id=%s, artifact_id=%s
                       WHERE (firm_id,upload_id,version_id)=(%s,%s,%s) AND clock_timestamp()<extraction_deadline""",
                    (state, assigned, artifact_id, *_identity(event)),
                ).rowcount
                if not updated:
                    raise _ExpiredAttempt
        except _ExpiredAttempt:
            return False
        return True

    def settle_staged(self, event: UploadCommitted) -> bool:
        """Settle a stage committed by any worker; an old in-memory ticket is unnecessary."""
        with self._db.transaction() as conn:
            row = self._upload(conn, event, lock=True)
            if row is None or row["state"] != "extracting" or row["staged_state"] is None:
                return False
            self._finish(conn, event, row, row["staged_state"])
            return True

    def recover_extractions(self, *, firm_id: str | None = None, limit: int = 100) -> int:
        """Finalize durable results or quarantine expired attempts; neither path repeats a provider request."""
        if not 1 <= limit <= 1000:
            raise ValueError("Extraction recovery page limit must be between 1 and 1000")
        recovered = 0
        for _ in range(limit):
            with self._db.transaction() as conn:
                row = conn.execute(
                    """SELECT * FROM prep_dispatch_uploads WHERE state='extracting'
                           AND (%s::text IS NULL OR firm_id=%s)
                           AND (staged_state IS NOT NULL OR extraction_deadline IS NULL OR extraction_deadline<=clock_timestamp())
                       ORDER BY extraction_deadline NULLS FIRST, received_at, firm_id, upload_id, version_id
                       LIMIT 1 FOR UPDATE SKIP LOCKED""", (firm_id, firm_id),
                ).fetchone()
                if row is None:
                    break
                event = UploadCommitted(row["firm_id"], row["upload_id"], row["version_id"])
                self._finish(conn, event, row, row["staged_state"] or "interrupted")
                recovered += 1
        return recovered

    def interrupt_extraction(self, ticket: ExtractionTicket) -> bool:
        """Record an uncertain unstaged request as terminal without authorizing another send."""
        with self._db.transaction() as conn:
            row = self._upload(conn, ticket.event, lock=True)
            if not self._active_attempt(row, ticket):
                return False
            self._finish(conn, ticket.event, row, "interrupted")
            return True

    def finish_without_extraction(self, event: UploadCommitted, *, return_id: str | None = None) -> bool:
        """Settle a denied upload; optionally supply an independently confirmed assignment."""
        with self._db.transaction() as conn:
            row = self._upload(conn, event, lock=True)
            if row is None or row["state"] != "pending":
                return False
            self._finish(conn, event, row, "no_authorization", return_id=return_id)
            return True

    def publish_pending(
        self, publisher: JobPublisher, *, firm_id: str | None = None, limit: int = 100,
    ) -> PublicationResult:
        """Replay a bounded page, outside the DB transaction, with stable broker deduplication keys."""
        if not 1 <= limit <= 1000:
            raise ValueError("Outbox page limit must be between 1 and 1000")
        with self._db.transaction() as conn:
            page = conn.execute(
                """SELECT firm_id, job_id FROM prep_job_outbox
                   WHERE published_at IS NULL AND (%s::text IS NULL OR firm_id=%s)
                   ORDER BY last_attempt_at NULLS FIRST, job_id LIMIT %s""",
                (firm_id, firm_id, limit),
            ).fetchall()
        published = 0
        for row in page:
            job = QueuedJob(**row)
            delivered = False
            try:
                publisher.publish(job, idempotency_key=job.job_id)
                delivered = True
                published += 1
            except Exception:
                # The broker may have accepted the message before its acknowledgement was lost.
                # Rotate failures behind untried items; the next pass uses the identical key.
                pass
            with self._db.transaction() as conn:
                conn.execute(
                    """UPDATE prep_job_outbox SET last_attempt_at = clock_timestamp(),
                               published_at = CASE WHEN %s THEN clock_timestamp() END
                       WHERE (firm_id, job_id) = (%s, %s) AND published_at IS NULL""",
                    (delivered, job.firm_id, job.job_id),
                )
        return PublicationResult(len(page), published)

    @staticmethod
    def _upload(conn: Connection[DictRow], event: UploadCommitted, *, lock: bool = False) -> DictRow | None:
        query = "SELECT * FROM prep_dispatch_uploads WHERE (firm_id,upload_id,version_id)=(%s,%s,%s)"
        return conn.execute(query + (" FOR UPDATE" if lock else ""), _identity(event)).fetchone()

    @staticmethod
    def _lock_return(conn: Connection[DictRow], firm_id: str, return_id: str) -> str | None:
        row = conn.execute(
            "SELECT profile_id FROM prep_returns WHERE (firm_id,return_id)=(%s,%s) FOR UPDATE",
            (firm_id, return_id),
        ).fetchone()
        return row["profile_id"] if row else None

    @staticmethod
    def _flag(conn: Connection[DictRow], firm_id: str, return_id: str) -> None:
        conn.execute(
            """UPDATE prep_returns SET review_required = true
               WHERE (firm_id, return_id) = (%s, %s) AND NOT review_required""",
            (firm_id, return_id),
        )

    @staticmethod
    def _db_now(conn: Connection[DictRow]) -> datetime:
        return conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]

    @staticmethod
    def _active_attempt(row: DictRow | None, ticket: ExtractionTicket) -> bool:
        return row is not None and (
            row["state"] == "extracting" and row["staged_state"] is None
            and row["attempt_id"] == ticket.attempt_id and row["grant_id"] == ticket.grant.grant_id
            and row["receipt_id"] == ticket.grant.receipt_id and ticket.event == ticket.grant.event
            and row["return_id"] == ticket.return_id
            and (row["object_id"], row["sha256"]) == (ticket.object_id, ticket.sha256)
            and row["extraction_policy"] == asdict(ticket.grant.policy)
            and row["grant_expires_at"] == ticket.grant.expires_at
        )

    @staticmethod
    def _authorization_failure(
        conn: Connection[DictRow], event: UploadCommitted, row: DictRow, grant_id: str | None,
        current: VerifiedUpload | None, grant: ExtractionGrant | None,
    ) -> str | None:
        if grant_id and (
            current is None or current.event != event or current.receipt_id != row["receipt_id"]
            or current.return_id != row["return_id"]
        ):
            return "misassigned"
        if (
            not grant_id or grant is None or grant.grant_id != grant_id or grant.event != event
            or grant.receipt_id != row["receipt_id"] or grant.expires_at.utcoffset() is None
            or grant.expires_at <= UploadDispatch._db_now(conn)
            or current is None or current.data_class != row["data_class"]
            or current.data_class not in {"synthetic", "approved_redacted"}
            or grant.policy.data_class != current.data_class
        ):
            return "no_authorization"
        return None

    def _finish(
        self, conn: Connection[DictRow], event: UploadCommitted, row: DictRow, state: str,
        *, return_id: str | None = None,
    ) -> None:
        firm_id = event.firm_id
        previous = row["return_id"]
        assigned = previous if previous is not None else return_id
        profile_id = self._lock_return(conn, firm_id, assigned) if assigned is not None else None
        if (assigned is not None and profile_id is None) or (
            previous is not None and return_id is not None and previous != return_id
        ):
            state = "misassigned"
        elif previous is None and assigned is not None:
            self._flag(conn, firm_id, assigned)
        conn.execute(
            """UPDATE prep_dispatch_uploads SET state=%s, return_id=%s, settled_at=clock_timestamp()
               WHERE (firm_id,upload_id,version_id)=(%s,%s,%s)""",
            (state, assigned, *_identity(event)),
        )
        if profile_id is not None and assigned is not None:
            self._freeze(conn, firm_id, assigned, profile_id)

    def _freeze(
        self, conn: Connection[DictRow], firm_id: str, return_id: str, profile_id: str,
    ) -> None:
        key = firm_id, return_id
        inputs = conn.execute(
            """SELECT upload_id,version_id,object_id,sha256,artifact_id,extraction_policy
               FROM prep_dispatch_uploads
               WHERE (firm_id,return_id)=(%s,%s) AND state='accepted' AND job_id IS NULL
                 AND NOT EXISTS (
                   SELECT 1 FROM prep_dispatch_uploads
                   WHERE (firm_id,return_id)=(%s,%s) AND state IN ('pending','extracting')
                 )
               ORDER BY upload_id,version_id""", (*key, *key),
        ).fetchall()
        if not inputs:
            return
        prior = conn.execute(
            """SELECT input_revision,revision_sequence FROM prep_jobs WHERE (firm_id,return_id)=(%s,%s)
               ORDER BY revision_sequence DESC,created_at DESC,job_id DESC LIMIT 1""", key,
        ).fetchone()
        sequence = prior["revision_sequence"] + 1 if prior else 1
        snapshot = {"parent_revision": prior["input_revision"] if prior else None, "inputs": inputs}
        if self._reasoning_policy is not None:
            snapshot["reasoning_policy"] = self._reasoning_policy
        job_id = uuid4().hex
        conn.execute(
            """INSERT INTO prep_jobs (firm_id,job_id,return_id,input_revision,profile_id,snapshot,revision_sequence)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (firm_id, job_id, return_id, _digest(snapshot), profile_id, Jsonb(snapshot), sequence),
        )
        conn.execute("INSERT INTO prep_job_outbox (firm_id,job_id) VALUES (%s,%s)", (firm_id, job_id))
        conn.execute(
            """UPDATE prep_dispatch_uploads SET job_id=%s
               WHERE (firm_id,return_id)=(%s,%s) AND state='accepted' AND job_id IS NULL""",
            (job_id, *key),
        )