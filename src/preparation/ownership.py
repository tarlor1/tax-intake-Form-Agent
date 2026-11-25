"""Reference PostgreSQL coordinator for return ownership and mutation journaling.

The caller supplies authenticated assignments and a firm-controlled NativeAuthority.
Proof IDs are opaque lookups, never assertions from an agent, a browser, or a
heartbeat. The authority must verify the current native checkout and separately
verify native close; recovery additionally requires the old session to be
quiescent, a complete read-only check under exclusive native checkout, and safe
release of that checkout. A PostgreSQL grant cannot enforce a native lock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import json
from typing import Literal, Protocol
from uuid import uuid4

from psycopg import Connection
from psycopg.errors import UniqueViolation
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from pydantic import JsonValue

from .coordinator_db import CoordinatorDB
from .tool_contract import ActionOutcome, Choice, JobScope, MutationDetails, MutationPermit, Observation, Verification


class OwnershipBlocked(RuntimeError):
    """The caller must stop acting on the native session."""


@dataclass(frozen=True, slots=True)
class OwnerGrant:
    firm_id: str
    return_id: str
    generation: int
    machine_id: str
    actor_id: str
    job_id: str | None
    review_id: str | None


@dataclass(frozen=True, slots=True)
class MutationIntent:
    step_id: str
    generation: int
    draft_key: str | None
    operation: dict
    before: JsonValue
    intended: JsonValue
    state: str
    immediate_readback: dict | None


@dataclass(frozen=True, slots=True)
class RecoveryEvidence:
    """Released, read-only inspection of the same return by the native authority.

    `complete` means the relevant search/inspection was exhaustive. The authority
    checks prior checkpoints in sequence, allowing later saved actions to supersede
    earlier ones, and supplies the normalized observed state for the pending step.
    It may return incomplete evidence only after verifying safe native release.
    """

    draft_key: str | None
    complete: bool
    checkpoints_match: bool
    observed: JsonValue = None


class NativeAuthority(Protocol):
    """Internal trust boundary; no default or pass-through proof verification.

    Every proof is bound to the exact firm, return, actor, session, and generation
    of the supplied grant. The deployment must resolve opaque IDs to independently
    verified native or firm checkout evidence. These synchronous, read-only lookups
    run within the return transaction; capture the underlying evidence separately.
    """

    def holds_checkout(self, grant: OwnerGrant, draft_key: str | None, proof_id: str) -> bool: ...
    def closed(self, grant: OwnerGrant, draft_key: str | None, proof_id: str) -> bool:
        """Independently verify native exit and release on the qualified session."""
        ...

    def existing_draft(self, grant: OwnerGrant, proof_id: str) -> str | None:
        """Verify unique, complete lookup of the exact unfiled draft under exclusive checkout."""
        ...

    def creation_allowed(self, grant: OwnerGrant, proof_id: str) -> bool:
        """Verify a complete, unique identity search proved no draft or filed return."""
        ...
    def created_draft(self, grant: OwnerGrant, step_id: str, proof_id: str) -> str | None:
        """Verify the new draft's exact identity and durable key under checkout."""
        ...
    def reopened(
        self, grant: OwnerGrant, draft_key: str | None, intents: tuple[MutationIntent, ...], proof_id: str,
    ) -> RecoveryEvidence | None:
        """Return None unless quiescence and safe native release are independently proven."""
        ...

    def restored_session(self, interrupted_grant: OwnerGrant, proof_id: str) -> bool:
        """Verify a newly qualified, safe session for the exact cordon episode."""
        ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS prep_machines (
    firm_id text NOT NULL,
    machine_id text NOT NULL,
    worker_id text NOT NULL,
    available boolean NOT NULL DEFAULT false,
    cordoned boolean NOT NULL DEFAULT false,
    PRIMARY KEY (firm_id, machine_id)
);
CREATE TABLE IF NOT EXISTS prep_machine_profiles (
    firm_id text NOT NULL,
    machine_id text NOT NULL,
    profile_id text NOT NULL,
    tax_year integer NOT NULL,
    store_id text NOT NULL,
    shared_store boolean NOT NULL,
    checkout_mode text NOT NULL CHECK (checkout_mode IN ('native', 'firm')),
    PRIMARY KEY (firm_id, machine_id, profile_id, tax_year, store_id),
    FOREIGN KEY (firm_id, machine_id) REFERENCES prep_machines (firm_id, machine_id)
);
CREATE TABLE IF NOT EXISTS prep_review_requests (
    request_order bigint GENERATED ALWAYS AS IDENTITY,
    firm_id text NOT NULL,
    review_id text NOT NULL,
    return_id text NOT NULL,
    machine_id text NOT NULL,
    reviewer_id text NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'owned', 'closed', 'cancelled')),
    PRIMARY KEY (firm_id, review_id),
    FOREIGN KEY (firm_id, return_id) REFERENCES prep_returns (firm_id, return_id),
    FOREIGN KEY (firm_id, machine_id) REFERENCES prep_machines (firm_id, machine_id)
);
CREATE INDEX IF NOT EXISTS prep_pending_reviews ON prep_review_requests (firm_id, return_id, request_order)
    WHERE state = 'pending';
CREATE TABLE IF NOT EXISTS prep_owners (
    firm_id text NOT NULL,
    return_id text NOT NULL,
    generation bigint NOT NULL,
    machine_id text NOT NULL,
    actor_id text NOT NULL,
    job_id text,
    review_id text,
    lease_until timestamptz,
    checkout_proof_id text,
    reconciling boolean NOT NULL DEFAULT false,
    released_at timestamptz,
    PRIMARY KEY (firm_id, return_id, generation),
    FOREIGN KEY (firm_id, return_id) REFERENCES prep_returns (firm_id, return_id),
    FOREIGN KEY (firm_id, machine_id) REFERENCES prep_machines (firm_id, machine_id),
    FOREIGN KEY (firm_id, job_id) REFERENCES prep_jobs (firm_id, job_id),
    FOREIGN KEY (firm_id, review_id) REFERENCES prep_review_requests (firm_id, review_id),
    CHECK ((job_id IS NOT NULL AND review_id IS NULL AND lease_until IS NOT NULL) OR
           (job_id IS NULL AND review_id IS NOT NULL AND lease_until IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS prep_one_return_owner ON prep_owners (firm_id, return_id) WHERE released_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS prep_one_machine_owner ON prep_owners (firm_id, machine_id) WHERE released_at IS NULL;
CREATE TABLE IF NOT EXISTS prep_owner_events (
    event_order bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    firm_id text NOT NULL,
    return_id text NOT NULL,
    generation bigint NOT NULL,
    event text NOT NULL,
    proof_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (firm_id, return_id, generation) REFERENCES prep_owners (firm_id, return_id, generation)
);
CREATE TABLE IF NOT EXISTS prep_steps (
    step_order bigint GENERATED ALWAYS AS IDENTITY,
    firm_id text NOT NULL,
    step_id text NOT NULL,
    job_id text NOT NULL,
    return_id text NOT NULL,
    generation bigint NOT NULL,
    profile_id text NOT NULL,
    draft_key text,
    operation jsonb NOT NULL,
    action jsonb,
    expected_prior jsonb NOT NULL,
    intended jsonb NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN
        ('pending', 'readback', 'reconciled_exact', 'reconciled_absent', 'reconciled_ambiguous')),
    immediate_readback jsonb,
    proof_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    acknowledged_at timestamptz,
    PRIMARY KEY (firm_id, step_id),
    FOREIGN KEY (firm_id, job_id) REFERENCES prep_jobs (firm_id, job_id),
    FOREIGN KEY (firm_id, return_id, generation) REFERENCES prep_owners (firm_id, return_id, generation)
);
CREATE INDEX IF NOT EXISTS prep_step_history ON prep_steps (firm_id, job_id, step_order);
CREATE UNIQUE INDEX IF NOT EXISTS prep_one_pending_step ON prep_steps (firm_id, job_id) WHERE state = 'pending';
CREATE TABLE IF NOT EXISTS prep_verifications (
    firm_id text NOT NULL,
    return_id text NOT NULL,
    generation bigint NOT NULL,
    job_id text NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (firm_id, return_id, generation),
    FOREIGN KEY (firm_id, return_id, generation) REFERENCES prep_owners (firm_id, return_id, generation),
    FOREIGN KEY (firm_id, job_id) REFERENCES prep_jobs (firm_id, job_id)
);
CREATE TABLE IF NOT EXISTS prep_write_blocks (
    firm_id text NOT NULL,
    return_id text NOT NULL,
    reason text NOT NULL,
    PRIMARY KEY (firm_id, return_id),
    FOREIGN KEY (firm_id, return_id) REFERENCES prep_returns (firm_id, return_id)
);
"""

_UPGRADE_SQL = """
ALTER TABLE prep_review_requests ADD COLUMN IF NOT EXISTS input_revision text;
ALTER TABLE prep_review_requests ADD COLUMN IF NOT EXISTS profile_id text;
ALTER TABLE prep_review_requests ADD COLUMN IF NOT EXISTS requested_latest boolean NOT NULL DEFAULT false;
ALTER TABLE prep_owner_events ADD COLUMN IF NOT EXISTS draft_key text;

-- Old requests carry no recoverable revision. Do not infer one or release an
-- existing native reservation; its original owner must prove native close.
UPDATE prep_review_requests SET state = 'cancelled' WHERE state = 'pending' AND input_revision IS NULL;
ALTER TABLE prep_review_requests DROP CONSTRAINT IF EXISTS prep_review_revision_scope;
ALTER TABLE prep_review_requests ADD CONSTRAINT prep_review_revision_scope
    FOREIGN KEY (firm_id, return_id, input_revision, profile_id)
    REFERENCES prep_jobs (firm_id, return_id, input_revision, profile_id);
ALTER TABLE prep_review_requests DROP CONSTRAINT IF EXISTS prep_review_revision_required;
ALTER TABLE prep_review_requests ADD CONSTRAINT prep_review_revision_required CHECK (
    (input_revision IS NOT NULL AND profile_id IS NOT NULL)
    OR (input_revision IS NULL AND profile_id IS NULL AND state <> 'pending')
);

CREATE OR REPLACE FUNCTION prep_keep_frozen_job() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Preparation jobs are immutable';
    END IF;
    IF (NEW.firm_id, NEW.job_id, NEW.return_id, NEW.input_revision, NEW.profile_id, NEW.snapshot,
        NEW.created_at, NEW.revision_sequence) IS DISTINCT FROM
       (OLD.firm_id, OLD.job_id, OLD.return_id, OLD.input_revision, OLD.profile_id, OLD.snapshot,
        OLD.created_at, OLD.revision_sequence) THEN
        RAISE EXCEPTION 'Preparation job inputs are immutable';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS prep_frozen_job ON prep_jobs;
CREATE TRIGGER prep_frozen_job BEFORE UPDATE OR DELETE ON prep_jobs
    FOR EACH ROW EXECUTE FUNCTION prep_keep_frozen_job();
"""


def install_ownership_schema(db: Connection[DictRow]) -> None:
    """Install ownership after the core migration has added the revision sequence."""
    db.execute(_SCHEMA)
    db.execute(_UPGRADE_SQL)


def _one(db, sql, args):
    return db.execute(sql, args).fetchone()


def _same_json(left, right):
    # Python otherwise considers boolean False equal to numeric zero.
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _job_state(db, grant, state):
    if grant.job_id is not None:
        db.execute("UPDATE prep_jobs SET state = %s WHERE firm_id = %s AND job_id = %s",
                   (state, grant.firm_id, grant.job_id))


def _event(db, grant, event, proof_id=None, *, draft_key=None):
    db.execute("""INSERT INTO prep_owner_events (firm_id, return_id, generation, event, proof_id, draft_key)
                  VALUES (%s, %s, %s, %s, %s, %s)""",
               (grant.firm_id, grant.return_id, grant.generation, event, proof_id, draft_key))


def _return(db, firm_id, return_id):
    row = _one(db, "SELECT * FROM prep_returns WHERE firm_id = %s AND return_id = %s FOR UPDATE",
               (firm_id, return_id))
    if row is None:
        raise OwnershipBlocked("unknown-return")
    return row


def _qualification(db, firm_id, machine_id, target):
    if target["pinned_machine_id"] not in (None, machine_id):
        return None
    return _one(db, """SELECT * FROM prep_machine_profiles WHERE firm_id = %s AND machine_id = %s
                    AND profile_id = %s AND tax_year = %s AND store_id = %s
                    AND (shared_store OR %s::text IS NOT NULL OR %s::text IS NULL)""",
                (firm_id, machine_id, target["profile_id"], target["tax_year"], target["store_id"],
                 target["pinned_machine_id"], target["draft_key"]))


def _machine(db, firm_id, machine_id):
    return _one(db, "SELECT * FROM prep_machines WHERE firm_id = %s AND machine_id = %s FOR UPDATE",
                (firm_id, machine_id))


def _current(db, firm_id, return_id):
    return _one(db, """SELECT *, (lease_until <= clock_timestamp()) IS TRUE AS expired FROM prep_owners
                    WHERE firm_id = %s AND return_id = %s AND released_at IS NULL""", (firm_id, return_id))


def _grant(row):
    return OwnerGrant(*(row[key] for key in (
        "firm_id", "return_id", "generation", "machine_id", "actor_id", "job_id", "review_id",
    )))


def _latest_started_job(db, target):
    return _one(db, """SELECT j.input_revision, j.state, j.revision_sequence, j.created_at, j.job_id FROM prep_jobs j
                    WHERE (j.firm_id, j.return_id, j.profile_id) = (%s, %s, %s)
                      AND (j.state <> 'queued' OR EXISTS (SELECT 1 FROM prep_owners o
                          WHERE (o.firm_id, o.job_id) = (j.firm_id, j.job_id)))
                    ORDER BY j.revision_sequence DESC, j.created_at DESC, j.job_id DESC LIMIT 1""",
                (target["firm_id"], target["return_id"], target["profile_id"]))


def _job_order(job):
    return job["revision_sequence"], job["created_at"], job["job_id"]


def _head_review(db, target):
    if target["draft_key"] is None:
        return None
    job = _latest_started_job(db, target)
    if job is None or job["state"] not in ("ready_for_review", "needs_attention"):
        return None
    # Only the native revision whose preparation ended can take priority.
    # A historical evidence view does not confer a native reservation.
    return _one(db, """SELECT r.* FROM prep_review_requests r
                    JOIN prep_machines m USING (firm_id, machine_id)
                    JOIN prep_machine_profiles p USING (firm_id, machine_id, profile_id)
                    WHERE (r.firm_id, r.return_id, r.profile_id, r.input_revision) = (%s, %s, %s, %s)
                      AND r.state = 'pending' AND p.tax_year = %s AND p.store_id = %s AND NOT m.cordoned
                      AND (p.shared_store OR m.machine_id = %s)
                      AND (%s::text IS NULL OR m.machine_id = %s)
                    ORDER BY r.request_order LIMIT 1""",
                (target["firm_id"], target["return_id"], target["profile_id"], job["input_revision"],
                 target["tax_year"], target["store_id"], target["pinned_machine_id"], target["pinned_machine_id"],
                 target["pinned_machine_id"]))


def _new_grant(db, firm_id, return_id, machine_id, actor_id, job_id, review_id, lease_seconds):
    if _one(db, "SELECT 1 FROM prep_owners WHERE firm_id = %s AND machine_id = %s AND released_at IS NULL",
            (firm_id, machine_id)):
        return None
    row = _one(db, """INSERT INTO prep_owners
                    (firm_id, return_id, generation, machine_id, actor_id, job_id, review_id, lease_until)
                    SELECT %s, %s, COALESCE(MAX(generation), 0) + 1, %s, %s, %s, %s,
                        clock_timestamp() + %s::integer * interval '1 second'
                    FROM prep_owners WHERE firm_id = %s AND return_id = %s RETURNING *""",
               (firm_id, return_id, machine_id, actor_id, job_id, review_id, lease_seconds, firm_id, return_id))
    grant = _grant(row)
    _event(db, grant, "claimed")
    return grant


def _require_grant(db, grant):
    target = _return(db, grant.firm_id, grant.return_id)
    # Both claims and existing-owner operations acquire return, then machine.
    machine = _machine(db, grant.firm_id, grant.machine_id)
    row = _current(db, grant.firm_id, grant.return_id)
    if row is None or _grant(row) != grant:
        raise OwnershipBlocked("stale-owner")
    return row, target, machine


def _scope(grant, target):
    if grant.job_id is None or target["draft_key"] is None:
        raise OwnershipBlocked("draft-not-bound")
    return JobScope(job_id=grant.job_id, owner_generation=grant.generation,
                    profile_id=target["profile_id"], draft_key=target["draft_key"])


def _lease(seconds):
    if type(seconds) is not int or not 1 <= seconds <= 86_400:
        raise ValueError("Lease must be between 1 and 86400 seconds")
    return seconds


def _release(db, grant, proof_id):
    db.execute("""UPDATE prep_owners SET released_at = clock_timestamp()
                  WHERE firm_id = %s AND return_id = %s AND generation = %s""",
               (grant.firm_id, grant.return_id, grant.generation))
    if grant.review_id is not None:
        db.execute("UPDATE prep_review_requests SET state = 'closed' WHERE firm_id = %s AND review_id = %s",
                   (grant.firm_id, grant.review_id))
    _event(db, grant, "released", proof_id)


def _pending(db, grant):
    return _one(db, "SELECT * FROM prep_steps WHERE firm_id = %s AND job_id = %s AND state = 'pending'",
                (grant.firm_id, grant.job_id))


def _job(db, grant):
    return _one(db, "SELECT * FROM prep_jobs WHERE firm_id = %s AND job_id = %s", (grant.firm_id, grant.job_id))


def _verification_state(db, grant):
    verification = _one(db, """SELECT result FROM prep_verifications
                            WHERE firm_id = %s AND return_id = %s AND generation = %s""",
                        (grant.firm_id, grant.return_id, grant.generation))
    if verification is None:
        return None
    return "needs_attention" if verification["result"]["issues"] else "ready_for_review"


class OwnershipStore:
    def __init__(self, db: CoordinatorDB, *, native_authority: NativeAuthority):
        self.db = db
        self.native = native_authority

    def install(self) -> None:
        with self.db.transaction() as db:
            install_ownership_schema(db)

    def register_machine(self, firm_id: str, machine_id: str, worker_id: str) -> None:
        with self.db.transaction() as db:
            db.execute("""INSERT INTO prep_machines (firm_id, machine_id, worker_id) VALUES (%s, %s, %s)
                          ON CONFLICT DO NOTHING""", (firm_id, machine_id, worker_id))
            if _machine(db, firm_id, machine_id)["worker_id"] != worker_id:
                raise OwnershipBlocked("machine-already-enrolled")

    def qualify_machine(
        self, firm_id: str, machine_id: str, profile_id: str, tax_year: int, store_id: str, *,
        shared_store: bool, checkout_mode: Literal["native", "firm"],
    ) -> None:
        """Internal enrollment, after approval of this build, store and checkout path."""
        with self.db.transaction() as db:
            db.execute("""INSERT INTO prep_machine_profiles VALUES (%s, %s, %s, %s, %s, %s, %s)
                          ON CONFLICT DO NOTHING""",
                       (firm_id, machine_id, profile_id, tax_year, store_id, shared_store, checkout_mode))
            row = _one(db, """SELECT shared_store, checkout_mode FROM prep_machine_profiles WHERE firm_id = %s
                            AND machine_id = %s AND profile_id = %s AND tax_year = %s AND store_id = %s""",
                       (firm_id, machine_id, profile_id, tax_year, store_id))
            if row != {"shared_store": shared_store, "checkout_mode": checkout_mode}:
                raise OwnershipBlocked("machine-qualification-conflict")

    def set_available(self, firm_id: str, machine_id: str, available: bool) -> None:
        """Record explicit approval/release of the interactive session for automation."""
        with self.db.transaction() as db:
            machine = _machine(db, firm_id, machine_id)
            if machine is None or (available and machine["cordoned"]):
                raise OwnershipBlocked("machine-unavailable")
            if available and _one(db, """SELECT 1 FROM prep_owners WHERE firm_id = %s AND machine_id = %s
                                        AND released_at IS NULL AND review_id IS NOT NULL""", (firm_id, machine_id)):
                raise OwnershipBlocked("review-session-in-use")
            db.execute("UPDATE prep_machines SET available = %s WHERE firm_id = %s AND machine_id = %s",
                       (available, firm_id, machine_id))

    def claim_worker(self, firm_id: str, job_id: str, machine_id: str, worker_id: str, *,
                     lease_seconds: int = 60) -> OwnerGrant | None:
        _lease(lease_seconds)
        with self.db.transaction() as db:
            job = _one(db, "SELECT return_id FROM prep_jobs WHERE firm_id = %s AND job_id = %s", (firm_id, job_id))
            if job is None:
                return None
            return_id = job["return_id"]
            target = _return(db, firm_id, return_id)
            if (_current(db, firm_id, return_id) or _head_review(db, target)
                    or _one(db, "SELECT 1 FROM prep_write_blocks WHERE firm_id = %s AND return_id = %s",
                            (firm_id, return_id))):
                return None
            job = _one(db, """SELECT job_id, state, profile_id, revision_sequence, created_at,
                                     snapshot ->> 'parent_revision' AS parent_revision
                              FROM prep_jobs WHERE (firm_id, return_id) = (%s, %s)
                                AND state NOT IN ('ready_for_review', 'needs_attention')
                              ORDER BY revision_sequence, created_at, job_id LIMIT 1""", (firm_id, return_id))
            if (job is None or job["job_id"] != job_id or job["state"] != "queued"
                    or job["profile_id"] != target["profile_id"]):
                return None
            sequence, parent = job["revision_sequence"], job["parent_revision"]
            predecessor = _one(db, """SELECT input_revision, revision_sequence, profile_id, state FROM prep_jobs
                                      WHERE (firm_id, return_id) = (%s, %s)
                                        AND (revision_sequence, created_at, job_id) < (%s, %s, %s)
                                      ORDER BY revision_sequence DESC, created_at DESC, job_id DESC LIMIT 1""",
                               (firm_id, return_id, *_job_order(job)))
            if predecessor is None:
                if sequence > 1 or parent is not None:
                    return None
            elif (predecessor["state"] not in ("ready_for_review", "needs_attention")
                    or predecessor["profile_id"] != job["profile_id"]
                    or ((sequence > 0 or parent is not None) and predecessor["input_revision"] != parent)
                    or (sequence > 0 and predecessor["revision_sequence"] != sequence - 1)):
                return None
            machine = _machine(db, firm_id, machine_id)
            if (machine is None or machine["worker_id"] != worker_id or not machine["available"]
                    or machine["cordoned"] or _qualification(db, firm_id, machine_id, target) is None):
                return None
            grant = _new_grant(db, firm_id, return_id, machine_id, worker_id, job_id, None, lease_seconds)
            if grant:
                _job_state(db, grant, "preparing")
            return grant

    def request_review(
        self, firm_id: str, return_id: str, machine_id: str, reviewer_id: str, *, input_revision: str | None = None,
    ) -> str:
        """Persist one exact revision; a legacy latest request does not follow new jobs."""
        with self.db.transaction() as db:
            target = _return(db, firm_id, return_id)
            machine = _machine(db, firm_id, machine_id)
            if machine is None or machine["cordoned"] or _qualification(db, firm_id, machine_id, target) is None:
                raise OwnershipBlocked("review-machine-ineligible")
            requested_latest = input_revision is None
            if requested_latest:
                prior = _one(db, """SELECT review_id FROM prep_review_requests
                                  WHERE (firm_id, return_id, machine_id, reviewer_id) = (%s, %s, %s, %s)
                                    AND requested_latest AND state IN ('pending', 'owned')
                                  ORDER BY request_order LIMIT 1""", (firm_id, return_id, machine_id, reviewer_id))
                if prior:
                    return prior["review_id"]
                # If nothing can be reviewed yet, freeze the oldest outstanding
                # preparation prerequisite instead of following future uploads.
                candidate = _latest_started_job(db, target)
                if candidate is None or (target["draft_key"] is None
                                         and candidate["state"] in ("ready_for_review", "needs_attention")):
                    candidate = _one(db, """SELECT input_revision FROM prep_jobs
                                           WHERE (firm_id, return_id, profile_id) = (%s, %s, %s)
                                             AND state NOT IN ('ready_for_review', 'needs_attention')
                                           ORDER BY revision_sequence, created_at, job_id LIMIT 1""",
                                     (firm_id, return_id, target["profile_id"]))
                if candidate is None:
                    raise OwnershipBlocked("review-revision-unavailable")
                input_revision = candidate["input_revision"]
            job = _one(db, """SELECT revision_sequence, created_at, job_id FROM prep_jobs
                             WHERE (firm_id, return_id, profile_id, input_revision) = (%s, %s, %s, %s)""",
                       (firm_id, return_id, target["profile_id"], input_revision))
            if job is None:
                raise OwnershipBlocked("review-revision-unavailable")
            if not requested_latest:
                started = _latest_started_job(db, target)
                if started and _job_order(job) < _job_order(started):
                    raise OwnershipBlocked("review-revision-superseded")
            prior = _one(db, """SELECT review_id, requested_latest FROM prep_review_requests
                              WHERE (firm_id, return_id, machine_id, reviewer_id, input_revision) =
                                    (%s, %s, %s, %s, %s) AND state IN ('pending', 'owned')
                              ORDER BY request_order LIMIT 1""",
                         (firm_id, return_id, machine_id, reviewer_id, input_revision))
            if prior:
                if requested_latest and not prior["requested_latest"]:
                    db.execute("""UPDATE prep_review_requests SET requested_latest = true
                                  WHERE (firm_id, review_id) = (%s, %s)""", (firm_id, prior["review_id"]))
                return prior["review_id"]
            review_id = uuid4().hex
            db.execute("""INSERT INTO prep_review_requests
                          (firm_id, review_id, return_id, machine_id, reviewer_id, input_revision, profile_id, requested_latest)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                       (firm_id, review_id, return_id, machine_id, reviewer_id, input_revision,
                        target["profile_id"], requested_latest))
            return review_id

    def find_review_request(self, firm_id: str, return_id: str, reviewer_id: str, input_revision: str) -> str | None:
        """Locate an existing request for recovery; this grants no ownership."""
        with self.db.transaction() as db:
            request = _one(db, """SELECT review_id FROM prep_review_requests
                                 WHERE (firm_id, return_id, reviewer_id, input_revision) = (%s, %s, %s, %s)
                                   AND state IN ('pending', 'owned')
                                 ORDER BY (state = 'owned') DESC, request_order LIMIT 1""",
                           (firm_id, return_id, reviewer_id, input_revision))
            return request["review_id"] if request else None

    def review_input_revision(self, grant: OwnerGrant) -> str:
        """Return the pinned input revision for a current native review grant."""
        with self.db.transaction() as db:
            _require_grant(db, grant)
            request = _one(db, """SELECT input_revision FROM prep_review_requests
                                 WHERE (firm_id, review_id) = (%s, %s) AND state = 'owned'""",
                           (grant.firm_id, grant.review_id))
            if request is None or request["input_revision"] is None:
                raise OwnershipBlocked("review-revision-unavailable")
            return request["input_revision"]

    def cancel_review(self, firm_id: str, return_id: str, review_id: str, reviewer_id: str) -> bool:
        with self.db.transaction() as db:
            _return(db, firm_id, return_id)
            return bool(db.execute("""UPDATE prep_review_requests SET state = 'cancelled' WHERE firm_id = %s
                                   AND return_id = %s AND review_id = %s AND reviewer_id = %s AND state = 'pending'""",
                                   (firm_id, return_id, review_id, reviewer_id)).rowcount)

    def claim_review(self, firm_id: str, return_id: str, review_id: str, reviewer_id: str) -> OwnerGrant | None:
        with self.db.transaction() as db:
            target = _return(db, firm_id, return_id)
            if _current(db, firm_id, return_id):
                return None
            request = _head_review(db, target)
            if request is None or (request["review_id"], request["reviewer_id"]) != (review_id, reviewer_id):
                return None
            machine_id = request["machine_id"]
            machine = _machine(db, firm_id, machine_id)
            if machine["cordoned"] or _qualification(db, firm_id, machine_id, target) is None:
                return None
            grant = _new_grant(db, firm_id, return_id, machine_id, reviewer_id, None, review_id, None)
            if grant:
                db.execute("UPDATE prep_review_requests SET state = 'owned' WHERE firm_id = %s AND review_id = %s",
                           (firm_id, review_id))
                db.execute("UPDATE prep_machines SET available = false WHERE firm_id = %s AND machine_id = %s",
                           (firm_id, machine_id))
            return grant

    def _dispatchable(self, db, grant, *, require_checkout=True):
        row, target, machine = _require_grant(db, grant)
        if (row["reconciling"] or row["expired"] or machine["cordoned"]
                or (grant.job_id is not None and not machine["available"])
                or _qualification(db, grant.firm_id, grant.machine_id, target) is None):
            raise OwnershipBlocked("owner-not-dispatchable")
        if require_checkout and (row["checkout_proof_id"] is None or not self.native.holds_checkout(
                grant, target["draft_key"], row["checkout_proof_id"])):
            raise OwnershipBlocked("checkout-unproven")
        return row, target

    def require_review_checkout(self, db: Connection[DictRow], grant: OwnerGrant) -> None:
        """Validate the native reservation while the caller records a review decision."""
        if grant.review_id is None:
            raise OwnershipBlocked("review-assignment-required")
        self._dispatchable(db, grant)

    def activate(self, grant: OwnerGrant, proof_id: str) -> None:
        """Bind the actual native checkout after the local session is acquired."""
        with self.db.transaction() as db:
            row, target = self._dispatchable(db, grant, require_checkout=False)
            if not proof_id or not self.native.holds_checkout(grant, target["draft_key"], proof_id):
                raise OwnershipBlocked("checkout-unproven")
            if row["checkout_proof_id"] != proof_id:
                db.execute("""UPDATE prep_owners SET checkout_proof_id = %s
                              WHERE firm_id = %s AND return_id = %s AND generation = %s""",
                           (proof_id, grant.firm_id, grant.return_id, grant.generation))
                _event(db, grant, "checkout", proof_id)

    def renew(self, grant: OwnerGrant, *, lease_seconds: int = 60) -> None:
        if grant.job_id is None:
            raise OwnershipBlocked("review-has-no-lease")
        with self.db.transaction() as db:
            self._dispatchable(db, grant, require_checkout=False)
            db.execute("""UPDATE prep_owners SET lease_until = clock_timestamp() + %s::integer * interval '1 second'
                          WHERE firm_id = %s AND return_id = %s AND generation = %s""",
                       (_lease(lease_seconds), grant.firm_id, grant.return_id, grant.generation))

    def scope(self, grant: OwnerGrant) -> JobScope:
        with self.db.transaction() as db:
            _, target = self._dispatchable(db, grant)
            return _scope(grant, target)

    def require_owner(self, grant: OwnerGrant, scope: JobScope) -> None:
        if scope != self.scope(grant):
            raise OwnershipBlocked("job-scope-mismatch")

    def checkpoints(self, grant: OwnerGrant) -> tuple[MutationIntent, ...]:
        """Load local plan checkpoints; skip exact effects instead of dispatching them again."""
        with self.db.transaction() as db:
            self._dispatchable(db, grant)
            if grant.job_id is None:
                raise OwnershipBlocked("worker-assignment-required")
            return self._step_history(db, grant)

    def _write_intent(self, db, grant, target, operation, details, action=None, proof_id=None):
        if grant.job_id is None or _job(db, grant)["state"] != "preparing":
            raise OwnershipBlocked("job-not-preparing")
        if _pending(db, grant):
            raise OwnershipBlocked("pending-native-intent")
        prior = db.execute("""SELECT state, (expected_prior = %s AND intended = %s) AS same
                             FROM prep_steps WHERE firm_id = %s AND job_id = %s
                             AND operation = %s ORDER BY step_order""",
                           (Jsonb(details.before), Jsonb(details.intended), grant.firm_id, grant.job_id,
                            Jsonb(operation))).fetchall()
        if prior:
            if prior[-1]["state"] == "reconciled_absent":
                if not prior[-1]["same"] or sum(step["same"] for step in prior) != 1:
                    raise OwnershipBlocked("native-retry-preconditions-changed")
            elif operation["kind"] != "save_draft" or any(step["same"] for step in prior):
                raise OwnershipBlocked("native-intent-already-recorded")
        step_id = uuid4().hex
        db.execute("""INSERT INTO prep_steps (firm_id, step_id, job_id, return_id, generation, profile_id,
                      draft_key, operation, action, expected_prior, intended, proof_id)
                      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                   (grant.firm_id, step_id, grant.job_id, grant.return_id, grant.generation, target["profile_id"],
                    target["draft_key"], Jsonb(operation), Jsonb(action) if action is not None else None,
                    Jsonb(details.before), Jsonb(details.intended), proof_id))
        return step_id

    def begin(self, grant: OwnerGrant, scope: JobScope, choice: Choice) -> MutationPermit:
        action, mutation = choice.action, choice.mutation
        if mutation is None or not action.mutates:
            raise OwnershipBlocked("not-a-mutation")
        with self.db.transaction() as db:
            _, target = self._dispatchable(db, grant)
            if scope != _scope(grant, target):
                raise OwnershipBlocked("job-scope-mismatch")
            # Observation/control references are ephemeral. De-duplicate on the
            # durable binding, the source record, or the profiled save scope.
            operation = {"kind": action.name, "target": (
                action.arguments.binding_id if action.name == "set_field" else
                sorted(field.binding_id for field in action.arguments.fields) if action.name == "set_fields" else
                action.arguments.source_record_id if action.name == "ensure_record" else choice.semantic_key
            )}
            step_id = self._write_intent(db, grant, target, operation, mutation, action.model_dump(mode="json"))
            return MutationPermit(scope=scope, step_id=step_id, action=action, details=mutation)

    def readback(self, grant: OwnerGrant, permit: MutationPermit, outcome: ActionOutcome, observed: Observation) -> None:
        with self.db.transaction() as db:
            _, target = self._dispatchable(db, grant)
            if not permit.scope == observed.scope == _scope(grant, target):
                raise OwnershipBlocked("job-scope-mismatch")
            step = _pending(db, grant)
            if (step is None or step["step_id"] != permit.step_id or step["generation"] != grant.generation
                    or step["action"] != permit.action.model_dump(mode="json")
                    or not _same_json(step["expected_prior"], permit.details.before)
                    or not _same_json(step["intended"], permit.details.intended)
                    or observed.observation_id == permit.action.observation_id
                    or outcome.status == "uncertain"):
                raise OwnershipBlocked("unconfirmed-native-readback")
            self._ack(db, grant, permit.step_id, {
                **outcome.model_dump(mode="json"), "observation_id": observed.observation_id,
            })

    def bind_existing_draft(self, grant: OwnerGrant, proof_id: str) -> str:
        """Bind independently proven native identity without a Create intent."""
        with self.db.transaction() as db:
            _, target = self._dispatchable(db, grant)
            if (grant.job_id is None or target["draft_key"] is not None or _pending(db, grant)
                    or _job(db, grant)["state"] != "preparing"):
                raise OwnershipBlocked("existing-draft-binding-not-allowed")
            key = self.native.existing_draft(grant, proof_id) if proof_id else None
            if key is None:
                raise OwnershipBlocked("existing-draft-unproven")
            if not self._bind_draft(db, grant, target, key):
                raise OwnershipBlocked("draft-already-assigned")
            db.execute("""UPDATE prep_owners SET checkout_proof_id = NULL
                          WHERE (firm_id, return_id, generation) = (%s, %s, %s)""",
                       (grant.firm_id, grant.return_id, grant.generation))
            _event(db, grant, "existing-draft-bound", proof_id, draft_key=key)
            return key

    def begin_create(self, grant: OwnerGrant, proof_id: str) -> str:
        with self.db.transaction() as db:
            _, target = self._dispatchable(db, grant)
            if grant.job_id is None or target["draft_key"] is not None or not proof_id or not self.native.creation_allowed(
                    grant, proof_id):
                raise OwnershipBlocked("create-not-authorized")
            identity = {key: target[key] for key in ("return_id", "profile_id", "store_id", "tax_year")}
            return self._write_intent(db, grant, target, {"kind": "create", "target": identity},
                                      MutationDetails(before=None, intended=identity), proof_id=proof_id)

    def _bind_draft(self, db, grant, target, key):
        if not isinstance(key, str) or not 0 < len(key) <= 500:
            raise OwnershipBlocked("invalid-draft-key")
        # The core unique key on firm/store/draft rejects aliasing another return.
        qualification = _qualification(db, grant.firm_id, grant.machine_id, target)
        pinned = target["pinned_machine_id"] if qualification["shared_store"] else grant.machine_id
        try:
            with db.transaction():
                db.execute("""UPDATE prep_returns SET draft_key = %s, pinned_machine_id = %s
                              WHERE firm_id = %s AND return_id = %s""", (key, pinned, grant.firm_id, grant.return_id))
        except UniqueViolation:
            return False
        return True

    def complete_create(self, grant: OwnerGrant, step_id: str, proof_id: str) -> None:
        with self.db.transaction() as db:
            _, target = self._dispatchable(db, grant)
            step = _pending(db, grant)
            if (step is None or step["step_id"] != step_id or step["generation"] != grant.generation
                    or step["operation"]["kind"] != "create" or target["draft_key"] is not None):
                raise OwnershipBlocked("create-intent-mismatch")
            key = self.native.created_draft(grant, step_id, proof_id) if proof_id else None
            if not self._bind_draft(db, grant, target, key):
                raise OwnershipBlocked("draft-already-assigned")
            self._ack(db, grant, step_id, {"status": "created", "draft_key": key})
            _event(db, grant, "draft-bound", proof_id)

    @staticmethod
    def _ack(db, grant, step_id, readback):
        db.execute("""UPDATE prep_steps SET state = 'readback', immediate_readback = %s,
                      acknowledged_at = clock_timestamp() WHERE firm_id = %s AND step_id = %s""",
                   (Jsonb(readback), grant.firm_id, step_id))

    def start_verification(self, grant: OwnerGrant) -> None:
        with self.db.transaction() as db:
            _, target = self._dispatchable(db, grant)
            _scope(grant, target)
            if _job(db, grant)["state"] != "preparing" or _pending(db, grant):
                raise OwnershipBlocked("job-not-ready-to-verify")
            _job_state(db, grant, "verifying")
            _event(db, grant, "verifying")

    def record_verification(
        self, grant: OwnerGrant, verification: Verification, *,
        apply: Callable[[Connection[DictRow], Verification], None] | None = None,
    ) -> None:
        """Persist independently reopened values and their evidence projection atomically."""
        with self.db.transaction() as db:
            _, target = self._dispatchable(db, grant)
            if verification.scope != _scope(grant, target) or _job(db, grant)["state"] != "verifying":
                raise OwnershipBlocked("verification-scope-mismatch")
            db.execute("""INSERT INTO prep_verifications (firm_id, return_id, generation, job_id, result)
                          VALUES (%s, %s, %s, %s, %s)""", (grant.firm_id, grant.return_id, grant.generation,
                                                            grant.job_id, Jsonb(verification.model_dump(mode="json"))))
            if apply is not None:
                apply(db, verification)

    def release(self, grant: OwnerGrant, proof_id: str, *, issue: str | None = None) -> None:
        """Release after verified native close (including the checkout), never UI/window loss alone."""
        with self.db.transaction() as db:
            row, target, _ = _require_grant(db, grant)
            if row["reconciling"] or row["expired"] or _pending(db, grant):
                raise OwnershipBlocked("reconciliation-required")
            if (not proof_id or _qualification(db, grant.firm_id, grant.machine_id, target) is None
                    or not self.native.closed(grant, target["draft_key"], proof_id)):
                raise OwnershipBlocked("native-close-unproven")
            if grant.job_id is not None:
                state = _verification_state(db, grant)
                if issue:
                    _event(db, grant, "issue:" + issue)
                    state = "needs_attention"
                if state is None:
                    raise OwnershipBlocked("verification-or-issue-required")
                _job_state(db, grant, state)
            _release(db, grant, proof_id)

    def interrupt(self, grant: OwnerGrant, reason: Literal["expired", "disconnected", "uncertain", "unsafe"]) -> None:
        if reason not in ("expired", "disconnected", "uncertain", "unsafe"):
            raise ValueError("Unknown interruption reason")
        with self.db.transaction() as db:
            row, _, _ = _require_grant(db, grant)
            if reason == "expired" and not row["expired"]:
                raise OwnershipBlocked("owner-not-expired")
            if row["reconciling"]:
                return
            db.execute("UPDATE prep_machines SET available = false, cordoned = true WHERE firm_id = %s AND machine_id = %s",
                       (grant.firm_id, grant.machine_id))
            db.execute("""UPDATE prep_owners SET reconciling = true WHERE firm_id = %s AND return_id = %s AND generation = %s""",
                       (grant.firm_id, grant.return_id, grant.generation))
            _job_state(db, grant, "reconciling")
            _event(db, grant, "interrupted:" + reason)

    def restore_machine(self, firm_id: str, machine_id: str, proof_id: str) -> None:
        """Administrative safety approval after reconciliation; still unavailable for dispatch."""
        with self.db.transaction() as db:
            machine = _machine(db, firm_id, machine_id)
            if machine is None or not machine["cordoned"]:
                raise OwnershipBlocked("machine-not-cordoned")
            if _one(db, """SELECT 1 FROM prep_owners WHERE firm_id = %s AND machine_id = %s
                           AND released_at IS NULL""", (firm_id, machine_id)):
                raise OwnershipBlocked("machine-still-owns-return")
            latest = _one(db, """SELECT o.* FROM prep_owners o JOIN prep_owner_events e
                                USING (firm_id, return_id, generation)
                                WHERE o.firm_id = %s AND o.machine_id = %s AND e.event LIKE 'interrupted:%%'
                                ORDER BY e.event_order DESC LIMIT 1""", (firm_id, machine_id))
            if latest is None or not proof_id or not self.native.restored_session(_grant(latest), proof_id):
                raise OwnershipBlocked("safe-session-unproven")
            db.execute("UPDATE prep_machines SET cordoned = false WHERE firm_id = %s AND machine_id = %s",
                       (firm_id, machine_id))
            _event(db, _grant(latest), "machine-restored", proof_id)

    @staticmethod
    def _step_history(db, grant):
        steps = db.execute("""SELECT step_id, generation, draft_key, operation, expected_prior, intended,
                              state, immediate_readback FROM prep_steps WHERE firm_id = %s AND job_id = %s
                              ORDER BY step_order""", (grant.firm_id, grant.job_id)).fetchall()
        return tuple(MutationIntent(step["step_id"], step["generation"], step["draft_key"], step["operation"],
                                    step["expected_prior"], step["intended"], step["state"],
                                    step["immediate_readback"]) for step in steps)

    def reconcile(self, grant: OwnerGrant, proof_id: str) -> Literal["exact", "absent", "ambiguous"]:
        with self.db.transaction() as db:
            row, target, _ = _require_grant(db, grant)
            if not row["reconciling"]:
                raise OwnershipBlocked("owner-not-reconciling")
            intents = self._step_history(db, grant)
            evidence = self.native.reopened(grant, target["draft_key"], intents, proof_id) if proof_id else None
            if evidence is None:
                raise OwnershipBlocked("quiescence-or-native-release-unproven")
            if grant.review_id is not None:
                if (not evidence.complete or evidence.draft_key != target["draft_key"]
                        or _qualification(db, grant.firm_id, grant.machine_id, target) is None):
                    raise OwnershipBlocked("native-close-unproven")
                _event(db, grant, "reconciled:exact", proof_id)
                _release(db, grant, proof_id)
                return "exact"
            pending = intents[-1] if intents and intents[-1].state == "pending" else None
            decision = "ambiguous"
            if evidence.complete and evidence.checkpoints_match:
                same_draft = evidence.draft_key == target["draft_key"]
                if pending is None:
                    if same_draft:
                        decision = "exact"
                elif _same_json(evidence.observed, pending.intended):
                    if pending.operation["kind"] == "create":
                        key = evidence.draft_key
                        if isinstance(key, str) and 0 < len(key) <= 500 and self._bind_draft(db, grant, target, key):
                            decision = "exact"
                    elif same_draft:
                        decision = "exact"
                elif same_draft and _same_json(evidence.observed, pending.before):
                    decision = "absent"
            retry_exhausted = decision == "absent" and any(
                step.state == "reconciled_absent" and step.operation == pending.operation
                and _same_json(step.before, pending.before) and _same_json(step.intended, pending.intended)
                for step in intents[:-1]
            )
            if pending is not None:
                db.execute("""UPDATE prep_steps SET state = %s, proof_id = %s, acknowledged_at = clock_timestamp()
                              WHERE firm_id = %s AND step_id = %s""",
                           ("reconciled_" + decision, proof_id, grant.firm_id, pending.step_id))
            if decision == "ambiguous" or retry_exhausted:
                db.execute("INSERT INTO prep_write_blocks VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                           (grant.firm_id, grant.return_id, "retry-exhausted" if retry_exhausted else "ambiguous-native-state"))
                _job_state(db, grant, "needs_attention")
            else:
                _job_state(db, grant, _verification_state(db, grant) or "queued")
            _event(db, grant, "reconciled:" + decision, proof_id)
            _release(db, grant, proof_id)
            return decision


class OwnershipJournal:
    """Agent adapter bound once to a trusted worker assignment and its central generation."""

    def __init__(
        self, store: OwnershipStore, grant: OwnerGrant, *,
        persist_verification: Callable[[Connection[DictRow], Verification], None] | None = None,
    ):
        if grant.job_id is None:
            raise OwnershipBlocked("worker-assignment-required")
        self._store = store
        self._grant = grant
        self._persist_verification = persist_verification

    async def require_owner(self, scope: JobScope) -> None:
        await asyncio.to_thread(self._store.require_owner, self._grant, scope)

    async def begin(self, scope: JobScope, choice: Choice) -> MutationPermit:
        return await asyncio.to_thread(self._store.begin, self._grant, scope, choice)

    async def readback(self, permit: MutationPermit, outcome: ActionOutcome, observed: Observation) -> None:
        await asyncio.to_thread(self._store.readback, self._grant, permit, outcome, observed)

    async def start_verification(self) -> None:
        """Runner hook before calling the host's independent reopen."""
        await asyncio.to_thread(self._store.start_verification, self._grant)

    async def record_verification(self, verification: Verification) -> None:
        """Runner hook for the scoped, post-reopen result."""
        await asyncio.to_thread(
            self._store.record_verification, self._grant, verification, apply=self._persist_verification,
        )
