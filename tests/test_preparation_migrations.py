"""Exercise coordinator upgrades in an isolated PostgreSQL schema."""

from datetime import UTC, datetime

import pytest
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from src.preparation.coordinator_db import migrate, require_current_schema


@pytest.fixture
def legacy_db(coordinator_db):
    # The shared fixture installs today's core. Recreate the pre-sequence table
    # so the migration has to add the column as well as backfill it.
    with coordinator_db.transaction() as conn:
        conn.execute("ALTER TABLE prep_jobs DROP COLUMN revision_sequence")
    return coordinator_db


def _return(conn, firm_id="practice-firm", return_id="return-a"):
    conn.execute(
        """INSERT INTO prep_returns (firm_id, return_id, profile_id, store_id, tax_year)
           VALUES (%s, %s, 'practice-web', 'practice-store', 2025)""",
        (firm_id, return_id),
    )


def _job(conn, revision, parent, *, job_id=None, day=1, firm_id="practice-firm",
         return_id="return-a", profile_id="practice-web"):
    job_id = job_id or revision
    conn.execute(
        """INSERT INTO prep_jobs
               (firm_id, job_id, return_id, input_revision, profile_id, snapshot, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (firm_id, job_id, return_id, revision, profile_id,
         Jsonb({"parent_revision": parent, "inputs": [{"artifact_id": f"synthetic-{job_id}"}]}),
         datetime(2025, 1, day, tzinfo=UTC)),
    )


def _jobs(conn):
    return conn.execute("SELECT * FROM prep_jobs ORDER BY firm_id, job_id").fetchall()


def _relations(conn):
    return conn.execute(
        """SELECT oid, relname FROM pg_class
           WHERE relnamespace = current_schema()::regnamespace ORDER BY relname""",
    ).fetchall()


def _history(conn):
    return conn.execute(
        "SELECT version, applied_at FROM prep_schema_migrations ORDER BY version",
    ).fetchall()


def test_migrate_backfills_revision_sequence_from_ancestry_not_timestamps(legacy_db):
    with legacy_db.transaction() as conn:
        _return(conn)
        _return(conn, return_id="return-b")
        _return(conn, firm_id="other-practice-firm")
        # Insert newest revision first and give every ancestor a later timestamp.
        _job(conn, "tip", "middle", day=1)
        _job(conn, "middle", "root", day=2)
        _job(conn, "root", None, day=3)
        _job(conn, "root", None, return_id="return-b", job_id="independent-root")
        _job(conn, "root", None, firm_id="other-practice-firm")
        original_jobs = _jobs(conn)

    with pytest.raises(RuntimeError, match="not migrated"):
        require_current_schema(legacy_db)
    migrate(legacy_db)
    require_current_schema(legacy_db)

    with legacy_db.transaction() as conn:
        jobs = _jobs(conn)
    assert {(job["firm_id"], job["job_id"]): job.pop("revision_sequence") for job in jobs} == {
        ("practice-firm", "root"): 1,
        ("practice-firm", "middle"): 2,
        ("practice-firm", "tip"): 3,
        ("practice-firm", "independent-root"): 1,
        ("other-practice-firm", "root"): 1,
    }
    assert jobs == original_jobs


@pytest.mark.parametrize("invalid_ancestry", ["duplicate-revision", "fork", "multiple-roots", "orphan", "cycle"])
def test_migrate_rejects_invalid_legacy_ancestry_and_rolls_back(legacy_db, invalid_ancestry):
    with legacy_db.transaction() as conn:
        _return(conn)
        _job(conn, "root", None)
        _job(conn, "child", "root")
        if invalid_ancestry == "duplicate-revision":
            # Legacy uniqueness was per profile, so the parent key can be ambiguous.
            _job(conn, "root", None, job_id="same-revision", profile_id="other-profile")
        elif invalid_ancestry == "fork":
            _job(conn, "sibling", "root")
        elif invalid_ancestry == "multiple-roots":
            _job(conn, "second-root", None)
        elif invalid_ancestry == "orphan":
            _job(conn, "orphan", "missing")
        elif invalid_ancestry == "cycle":
            _job(conn, "cycle-a", "cycle-b")
            _job(conn, "cycle-b", "cycle-a")
        original_jobs = _jobs(conn)
        original_relations = _relations(conn)

    # A fork or multiple roots can be rejected by the new unique sequence index.
    with pytest.raises(
        (RuntimeError, UniqueViolation), match="legacy input revision ancestry|prep_job_revision_sequence",
    ):
        migrate(legacy_db)

    with legacy_db.transaction() as conn:
        assert _jobs(conn) == original_jobs
        assert _relations(conn) == original_relations
    with pytest.raises(RuntimeError, match="not migrated"):
        require_current_schema(legacy_db)


def test_repeated_migrate_preserves_job_rows_and_applied_migrations(legacy_db):
    with legacy_db.transaction() as conn:
        _return(conn)
        _job(conn, "root", None)

    version = migrate(legacy_db)
    with legacy_db.transaction() as conn:
        history = _history(conn)
        jobs = _jobs(conn)
        relations = _relations(conn)
    assert version > 0
    assert [row["version"] for row in history] == list(range(1, version + 1))
    assert {row["relname"] for row in relations} >= {
        "prep_job_revision_sequence", "prep_upload_batches", "prep_job_outbox", "prep_owners",
        "prep_return_directory", "prep_reasoning_usage", "tax_revisions",
    }

    assert migrate(legacy_db) == version
    require_current_schema(legacy_db)
    with legacy_db.transaction() as conn:
        assert _history(conn) == history
        assert _jobs(conn) == jobs
        assert _relations(conn) == relations
