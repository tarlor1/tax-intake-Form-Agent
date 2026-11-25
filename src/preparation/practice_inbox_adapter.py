"""Deliver analyzed, authorized public samples to the practice preparation queue."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from psycopg import Connection
from psycopg.rows import DictRow

from .batches import BatchSlotView, BatchView, InboxAnalysis, InboxPractice, IngestScope
from .coordinator_db import CoordinatorDB
from .local_document_index import ANALYSIS_VERSION
from .local_practice import PUBLIC_SAMPLE_YEARS, approval_recipient

if TYPE_CHECKING:
    from .local_practice_worker import LocalPracticeWorker


class PracticeInboxAdapter:
    approval_label = "Allow Gemini to prepare eligible public samples in the local practice browser"
    description = ("Documents are extracted locally. Only known public samples can enter Gemini practice; "
                   "Gemini receives guarded action choices, not the original documents.")

    def __init__(self, db: CoordinatorDB, worker: LocalPracticeWorker):
        self._db = db
        self._worker = worker
        self.recipient = approval_recipient(worker.policy)
        recipient = self.recipient
        self._recipient_key = (recipient.provider_id, recipient.account_id, recipient.model_id,
                               recipient.endpoint, recipient.data_class)
        with db.transaction() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS local_practice_submissions (
                    firm_id text NOT NULL,
                    upload_id text NOT NULL,
                    version_id text NOT NULL,
                    PRIMARY KEY (firm_id, upload_id, version_id),
                    FOREIGN KEY (firm_id, upload_id, version_id) REFERENCES prep_upload_slots (firm_id, upload_id, version_id)
                )
            """)

    def _submit(self, conn: Connection[DictRow], row: DictRow, analysis: InboxAnalysis, expires_at: float) -> None:
        self._worker.enqueue(
            conn, upload_id=row["upload_id"], version_id=row["version_id"], digest=row["digest"],
            analysis=analysis, tax_year=PUBLIC_SAMPLE_YEARS[row["digest"]], approval_expires_at=expires_at,
        )
        conn.execute("""
            INSERT INTO local_practice_submissions (firm_id, upload_id, version_id)
            VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
        """, (row["firm_id"], row["upload_id"], row["version_id"]))

    def dispatch_one(self, scope: IngestScope) -> bool:
        with self._db.transaction() as conn:
            row = conn.execute("""
                SELECT s.firm_id, s.upload_id, s.version_id, s.digest, a.category, a.result, b.consent_expires_at
                FROM local_document_analysis a
                JOIN prep_upload_slots s USING (firm_id, upload_id, version_id)
                JOIN prep_upload_batches b USING (firm_id, actor_id, batch_id)
                LEFT JOIN local_practice_submissions sent USING (firm_id, upload_id, version_id)
                WHERE s.firm_id=%s AND s.actor_id=%s AND a.analysis_version=%s AND b.approved
                  AND s.assigned_client_id IS NOT NULL
                  AND s.digest=ANY(%s) AND b.consent_expires_at > clock_timestamp() AND sent.upload_id IS NULL
                  AND (b.policy_provider, b.policy_account, b.policy_model, b.policy_endpoint, b.policy_class)=(%s,%s,%s,%s,%s)
                ORDER BY b.created_at, s.slot LIMIT 1 FOR UPDATE OF s SKIP LOCKED
            """, (scope.firm_id, scope.actor_id, ANALYSIS_VERSION, list(PUBLIC_SAMPLE_YEARS),
                  *self._recipient_key)).fetchone()
            if row is None:
                return False
            analysis = InboxAnalysis.model_validate({**row["result"], "category": row["category"]})
            self._submit(conn, row, analysis, row["consent_expires_at"].timestamp())
            return True

    def approve(self, scope: IngestScope, batch_id: str, slot_id: str) -> None:
        with self._db.transaction() as conn:
            row = conn.execute("""
                SELECT s.firm_id, s.upload_id, s.version_id, s.digest, a.result, a.category, clock_timestamp() AS now
                FROM prep_upload_slots s JOIN local_document_analysis a USING (firm_id, upload_id, version_id)
                WHERE (s.firm_id,s.actor_id,s.batch_id,s.slot)=(%s,%s,%s,%s) AND a.analysis_version=%s
                  AND s.assigned_client_id IS NOT NULL
                FOR UPDATE OF s
            """, (scope.firm_id, scope.actor_id, batch_id, slot_id, ANALYSIS_VERSION)).fetchone()
            if row is None or row["digest"] not in PUBLIC_SAMPLE_YEARS:
                raise ReviewError(409, "Only verified public sample originals are eligible for Gemini practice.")
            analysis = InboxAnalysis.model_validate({**row["result"], "category": row["category"]})
            if not analysis.facts:
                raise ReviewError(409, "No verified source boxes are available for practice preparation.")
            self._submit(conn, row, analysis, (row["now"] + timedelta(hours=1)).timestamp())

    def annotate(self, scope: IngestScope, batches: tuple[BatchView, ...]) -> tuple[BatchView, ...]:
        uploads = [slot.upload_id for batch in batches for slot in batch.files
                   if slot.upload_id and slot.assignment and slot.analysis and slot.analysis.status != "pending"]
        if not uploads:
            return batches
        with self._db.transaction() as conn:
            eligible = {(row["upload_id"], row["version_id"]): row["approved"] for row in conn.execute("""
                SELECT s.upload_id, s.version_id, (
                    b.approved AND b.consent_expires_at > clock_timestamp() AND
                    (b.policy_provider, b.policy_account, b.policy_model, b.policy_endpoint, b.policy_class)=(%s,%s,%s,%s,%s)
                ) AS approved
                FROM prep_upload_slots s JOIN prep_upload_batches b USING (firm_id, actor_id, batch_id)
                WHERE s.firm_id=%s AND s.actor_id=%s AND s.upload_id=ANY(%s) AND s.digest=ANY(%s)
            """, (*self._recipient_key, scope.firm_id, scope.actor_id, uploads, list(PUBLIC_SAMPLE_YEARS)))}

        def project(slot: BatchSlotView) -> BatchSlotView:
            key = slot.upload_id, slot.version_id
            if not slot.upload_id or not slot.analysis or key not in eligible:
                return slot
            status = self._worker.status(slot.upload_id)
            if status is not None:
                practice = InboxPractice.model_validate(status)
            elif eligible[key]:
                # Analysis is already durable; keep polling until its separately scheduled job appears.
                practice = InboxPractice(status="waiting", message="Approved. Waiting for the preparation queue.")
            elif slot.analysis.facts:
                practice = InboxPractice(status="waiting", gated="Approve Gemini to prepare this public practice sample.")
            else:
                practice = InboxPractice(status="needs_review", message="No verified source boxes are available for practice preparation.")
            return slot.model_copy(update={"practice": practice})

        return tuple(batch.model_copy(update={"files": tuple(project(slot) for slot in batch.files)}) for batch in batches)

    async def run_once(self) -> str | None:
        return await self._worker.run_once()
