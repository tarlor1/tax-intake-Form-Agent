"""Consume committed inbox originals and file their analysis by document category."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from collections.abc import Callable

from psycopg.types.json import Jsonb

from .batches import BatchView, InboxAnalysis, IngestScope, ImmutableObjects, MediaType
from .coordinator_db import CoordinatorDB

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_document_analysis (
    firm_id text NOT NULL,
    upload_id text NOT NULL,
    version_id text NOT NULL,
    category text,
    result jsonb NOT NULL,
    PRIMARY KEY (firm_id, upload_id, version_id),
    FOREIGN KEY (firm_id, upload_id, version_id) REFERENCES prep_upload_slots (firm_id, upload_id, version_id)
);
ALTER TABLE local_document_analysis ADD COLUMN IF NOT EXISTS analysis_version integer NOT NULL DEFAULT 1;
"""
_PENDING = InboxAnalysis(status="pending")
ANALYSIS_VERSION = 2


@dataclass(frozen=True, slots=True)
class LocalDocumentIndex:
    db: CoordinatorDB
    scope: IngestScope
    objects: ImmutableObjects
    analyze: Callable[[bytes, MediaType], dict]

    def install(self) -> None:
        with self.db.transaction() as conn:
            conn.execute(_SCHEMA)

    def process_one(self) -> bool:
        # Keep the outbox row locked through analysis and commit the result with
        # the acknowledgement. An interrupted process leaves the original eligible.
        with self.db.transaction() as conn:
            row = conn.execute("""
                SELECT o.upload_id, o.version_id, s.object_id, s.media_type
                FROM prep_upload_outbox o
                JOIN prep_upload_slots s USING (firm_id, upload_id, version_id)
                LEFT JOIN local_document_analysis a USING (firm_id, upload_id, version_id)
                WHERE o.firm_id=%s AND s.actor_id=%s
                  AND (a.analysis_version IS NULL OR a.analysis_version < %s)
                ORDER BY o.created_at, o.upload_id LIMIT 1 FOR UPDATE OF o SKIP LOCKED
            """, (self.scope.firm_id, self.scope.actor_id, ANALYSIS_VERSION)).fetchone()
            if row is None:
                return False

            result = self._analyze(row["object_id"], row["media_type"])
            key = self.scope.firm_id, row["upload_id"], row["version_id"]
            conn.execute("""
                INSERT INTO local_document_analysis (firm_id,upload_id,version_id,category,result,analysis_version)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (firm_id,upload_id,version_id) DO UPDATE
                  SET category=EXCLUDED.category, result=EXCLUDED.result, analysis_version=EXCLUDED.analysis_version
            """, (*key, result.category, Jsonb(result.model_dump(mode="json", exclude={"category"})), ANALYSIS_VERSION))
            conn.execute("""
                UPDATE prep_upload_outbox SET published_at=clock_timestamp()
                WHERE (firm_id,upload_id,version_id)=(%s,%s,%s)
            """, key)
            return True

    def _analyze(self, object_id: str, media_type: MediaType) -> InboxAnalysis:
        try:
            content = self.objects.read(self.scope.firm_id, object_id)
            if content is None:
                return InboxAnalysis(status="failed", message="The committed original could not be read.")
            return InboxAnalysis.model_validate(self.analyze(content, media_type))
        except Exception as error:
            # Never log document text or model/parse output. A bad file must not
            # continually block all later originals in the queue.
            log.warning("Local document extraction failed (%s)", type(error).__name__)
            return InboxAnalysis(status="failed", message="Local extraction failed. Download the original for review.")

    def annotate(self, batches: tuple[BatchView, ...]) -> tuple[BatchView, ...]:
        upload_ids = [slot.upload_id for batch in batches for slot in batch.files if slot.upload_id is not None]
        if not upload_ids:
            return batches
        with self.db.transaction() as conn:
            rows = conn.execute("""
                SELECT s.upload_id,s.version_id,a.category,a.result
                FROM prep_upload_slots s
                LEFT JOIN local_document_analysis a
                  ON (a.firm_id,a.upload_id,a.version_id)=(s.firm_id,s.upload_id,s.version_id)
                  AND a.analysis_version=%s
                WHERE s.firm_id=%s AND s.actor_id=%s AND s.upload_id=ANY(%s)
            """, (ANALYSIS_VERSION, self.scope.firm_id, self.scope.actor_id, upload_ids)).fetchall()
        updates = {}
        for row in rows:
            key = row["upload_id"], row["version_id"]
            result = (_PENDING if row["result"] is None else
                      InboxAnalysis.model_validate({**row["result"], "category": row["category"]}))
            updates[key] = {"analysis": result}
        return tuple(batch.model_copy(update={"files": tuple(
            slot.model_copy(update=updates.get((slot.upload_id, slot.version_id), {"analysis": _PENDING}))
            if slot.state == "uploaded" else slot for slot in batch.files
        )}) for batch in batches)
