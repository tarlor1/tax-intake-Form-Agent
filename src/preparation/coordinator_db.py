"""Connection and explicit schema migration boundary for the firm coordinator."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from psycopg import Connection
from psycopg import connect
from psycopg.rows import DictRow
from psycopg.rows import dict_row


_CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS prep_returns (
    firm_id text NOT NULL,
    return_id text NOT NULL,
    profile_id text NOT NULL,
    store_id text NOT NULL,
    tax_year integer NOT NULL CHECK (tax_year BETWEEN 1900 AND 2200),
    draft_key text,
    pinned_machine_id text,
    review_required boolean NOT NULL DEFAULT false,
    PRIMARY KEY (firm_id, return_id),
    UNIQUE (firm_id, store_id, draft_key)
);

CREATE TABLE IF NOT EXISTS prep_jobs (
    firm_id text NOT NULL,
    job_id text NOT NULL,
    return_id text NOT NULL,
    input_revision text NOT NULL,
    profile_id text NOT NULL,
    revision_sequence bigint NOT NULL DEFAULT 0 CHECK (revision_sequence >= 0),
    state text NOT NULL DEFAULT 'queued' CHECK (state IN (
        'queued', 'preparing', 'verifying', 'ready_for_review', 'needs_attention', 'reconciling'
    )),
    snapshot jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (firm_id, job_id),
    UNIQUE (firm_id, return_id, input_revision, profile_id),
    FOREIGN KEY (firm_id, return_id) REFERENCES prep_returns (firm_id, return_id)
);
"""

_RETURN_DIRECTORY = """
CREATE TABLE IF NOT EXISTS prep_return_directory (
    firm_id text NOT NULL,
    return_id text NOT NULL,
    client_id text NOT NULL,
    client_name text NOT NULL,
    client_reference text NOT NULL,
    return_type text NOT NULL CHECK (return_type IN ('individual', 'business', '1120')),
    product text NOT NULL CHECK (product IN ('web', 'desktop')),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (firm_id, return_id),
    FOREIGN KEY (firm_id, return_id) REFERENCES prep_returns (firm_id, return_id)
);
"""

_REASONING_USAGE = """
CREATE TABLE IF NOT EXISTS prep_reasoning_usage (
    firm_id text NOT NULL,
    job_id text NOT NULL,
    reservations integer NOT NULL CHECK (reservations > 0),
    PRIMARY KEY (firm_id, job_id),
    FOREIGN KEY (firm_id, job_id) REFERENCES prep_jobs (firm_id, job_id)
);
"""

_ADD_REVISION_SEQUENCE = """
ALTER TABLE prep_jobs ADD COLUMN IF NOT EXISTS
    revision_sequence bigint NOT NULL DEFAULT 0 CHECK (revision_sequence >= 0);

-- Recover old ordering from the recorded ancestry, not timestamps. An orphaned,
-- branched, or ambiguous chain requires investigation before enabling writers.
WITH RECURSIVE lineage AS (
    SELECT firm_id, return_id, job_id, input_revision, 1::bigint AS sequence
    FROM prep_jobs WHERE snapshot->>'parent_revision' IS NULL
    UNION ALL
    SELECT child.firm_id, child.return_id, child.job_id, child.input_revision, parent.sequence + 1
    FROM prep_jobs child JOIN lineage parent USING (firm_id, return_id)
    WHERE child.snapshot->>'parent_revision'=parent.input_revision
)
UPDATE prep_jobs job SET revision_sequence=lineage.sequence FROM lineage
WHERE (job.firm_id,job.job_id)=(lineage.firm_id,lineage.job_id) AND job.revision_sequence=0;

CREATE UNIQUE INDEX IF NOT EXISTS prep_job_revision_sequence
    ON prep_jobs (firm_id, return_id, revision_sequence) WHERE revision_sequence > 0;
"""

_MIGRATION_VERSION = 4
_MIGRATION_LOCK = 194796043


def connect_coordinator(dsn: str, *, schema: str) -> CoordinatorDB:
    """Connect to an explicitly provisioned schema; never fall back to public."""
    if not dsn.strip() or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
        raise ValueError("An explicit database DSN and a simple coordinator schema name are required.")
    if schema == "public" or schema.startswith("pg_"):
        raise ValueError("Use a dedicated coordinator schema.")

    def open_connection() -> Connection[DictRow]:
        connection = connect(dsn, options=f"-csearch_path={schema},pg_catalog", row_factory=dict_row)
        try:
            if connection.execute("SELECT current_schema() AS name").fetchone()["name"] != schema:
                raise RuntimeError("The dedicated coordinator schema is not provisioned or accessible.")
            connection.rollback()
        except BaseException:
            connection.close()
            raise
        return connection

    return CoordinatorDB(open_connection)


def migrate(db: CoordinatorDB) -> int:
    """Upgrade a provisioned coordinator atomically, with no licensed operations."""
    from .batches import install_batch_schema
    from .dispatch import install_dispatch_schema
    from .ownership import install_ownership_schema
    from .tax_store import install_tax_schema

    with db.transaction() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        connection.execute("""
            CREATE TABLE IF NOT EXISTS prep_schema_migrations (
                version integer PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )
        """)
        applied = tuple(row["version"] for row in connection.execute(
            "SELECT version FROM prep_schema_migrations ORDER BY version"
        ))
        if applied != tuple(range(1, len(applied) + 1)) or len(applied) > _MIGRATION_VERSION:
            raise RuntimeError("The coordinator schema history is unsupported.")

        def record(version: int) -> None:
            connection.execute("INSERT INTO prep_schema_migrations (version) VALUES (%s)", (version,))

        if len(applied) < 1:
            connection.execute(_CORE_SCHEMA)
            record(1)
        if len(applied) < 2:
            # Old frozen-job triggers did not cover the new sequence column.
            # Refuse ambiguous parent references before traversing them.
            if connection.execute("""
                SELECT 1 FROM prep_jobs GROUP BY firm_id, return_id, input_revision HAVING count(*) > 1 LIMIT 1
            """).fetchone():
                raise RuntimeError("Ambiguous legacy input revision ancestry; migrate after reconciliation.")
            connection.execute(_ADD_REVISION_SEQUENCE)
            if connection.execute("SELECT 1 FROM prep_jobs WHERE revision_sequence=0 LIMIT 1").fetchone():
                raise RuntimeError("Incomplete legacy input revision ancestry; migrate after reconciliation.")
            install_batch_schema(connection)
            install_dispatch_schema(connection)
            install_ownership_schema(connection)
            record(2)
        if len(applied) < 3:
            connection.execute(_RETURN_DIRECTORY)
            connection.execute(_REASONING_USAGE)
            install_tax_schema(connection)
            record(3)
        if len(applied) < 4:
            install_batch_schema(connection)
            record(4)
    return _MIGRATION_VERSION


def require_current_schema(db: CoordinatorDB) -> None:
    """Services never run a migration as a side effect of starting."""
    with db.transaction() as connection:
        if connection.execute("SELECT to_regclass('prep_schema_migrations') AS name").fetchone()["name"] is None:
            raise RuntimeError("The coordinator is not migrated. Run the explicit migrate command first.")
        applied = tuple(row["version"] for row in connection.execute(
            "SELECT version FROM prep_schema_migrations ORDER BY version"
        ))
        if applied != tuple(range(1, _MIGRATION_VERSION + 1)):
            raise RuntimeError("The coordinator schema is not compatible with this release.")


class CoordinatorDB:
    def __init__(self, connect: Callable[[], Connection[DictRow]]) -> None:
        self._connect = connect

    @contextmanager
    def transaction(self) -> Iterator[Connection[DictRow]]:
        """Commit only on clean exit, even if the connection factory uses autocommit."""
        with self._connect() as connection, connection.transaction():
            yield connection

    def install_core(self) -> None:
        """Install the reference core in a pre-provisioned, exclusive schema."""
        with self.transaction() as connection:
            connection.execute(_CORE_SCHEMA)
