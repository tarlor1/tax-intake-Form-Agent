"""Synthetic committed practice uploads feed the disposable PostgreSQL coordinator."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

import pytest
from psycopg.errors import CheckViolation

from sigma_practice_uploads import PracticeUploads, UploadSlot
from src.preparation.agent import StagePolicy
from src.preparation.dispatch import (
    ExtractionGrant,
    ExtractionPolicy,
    ExtractionTicket,
    PreparedExtraction,
    PublicationResult,
    QueuedJob,
    UploadCommitted,
    UploadDispatch,
    VerifiedUpload,
)
from src.preparation.providers import ProviderIdentity

FIRM = "practice-firm"
FOREIGN = "foreign-firm"
GRANT = "practice-grant"
POLICY = ExtractionPolicy("provider", "approved-account", "approved-model", "synthetic", "https://example.test")


class PracticeSource:
    """Use the fixture's durable feed, not the test caller's knowledge of a write."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.expires_at = datetime.now(UTC) + timedelta(hours=1)

    def _candidate(self, event):
        for entry in self.upstream.feed():
            if entry.version_id == event.version_id:
                candidate = self.upstream.candidate(entry)
                if candidate and (candidate.firm_id, candidate.slot_id) == (event.firm_id, event.upload_id):
                    return candidate
        return None

    def verify(self, event):
        candidate = self._candidate(event)
        if candidate is None:
            return None
        original = self.upstream.read_version(candidate.firm_id, candidate.version_id)
        if original is None:
            return None
        return VerifiedUpload(
            event, candidate.batch_id, candidate.version_id, sha256(original).hexdigest(),
            candidate.return_id, "synthetic",
        )

    def verify_grant(self, event, grant_id):
        candidate = self._candidate(event)
        if grant_id != GRANT or candidate is None or not candidate.authorized:
            return None
        return ExtractionGrant(grant_id, event, self.verify(event).receipt_id, POLICY, self.expires_at)


@pytest.fixture
def setup_dispatch(coordinator_db, tmp_path):
    coordinator_db.install_core()
    source = PracticeSource(PracticeUploads(tmp_path / "practice-upstream.sqlite3"))
    dispatch = UploadDispatch(coordinator_db, source)
    dispatch.install()
    with coordinator_db.transaction() as conn:
        for firm, return_id in ((FIRM, "a"), (FIRM, "b"), (FOREIGN, "foreign")):
            conn.execute(
                """INSERT INTO prep_returns (firm_id, return_id, profile_id, store_id, tax_year)
                   VALUES (%s, %s, %s, %s, 2025)""",
                (firm, return_id, "profile-" + return_id, "store"),
            )
    return dispatch, source


def _slot(upload_id, return_id=None, *, grant=GRANT):
    return UploadSlot(upload_id, return_id, None if return_id is None else 1, grant)


def _put(writer, batch, slot, *, firm=FIRM):
    version = writer.put_file(firm, batch, slot, f"Synthetic input for {batch}/{slot}".encode())
    return UploadCommitted(firm, slot, version)


def _commit(upstream, batch, *slots, firm=FIRM):
    with upstream.writer() as writer:
        writer.declare_batch(firm, batch, slots, approved_grants=(GRANT,))
        return tuple(_put(writer, batch, slot.slot_id, firm=firm) for slot in slots)


def _complete(dispatch, event, return_id):
    ticket = _claim(dispatch, event)
    assert dispatch.stage_extraction(ticket, PreparedExtraction(return_id, lambda _: f"facts-{event.version_id}"))
    assert dispatch.settle_staged(event)
    return ticket


def _claim(dispatch, event, *, disclose=True):
    ticket = dispatch.begin_extraction(event, GRANT, POLICY)
    assert isinstance(ticket, ExtractionTicket)
    if disclose:
        assert dispatch.authorize_disclosure(ticket) == "authorized"
    return ticket


def _rows(db, sql, params=()):
    with db.transaction() as conn:
        return conn.execute(sql, params).fetchall()


def _jobs(db):
    return _rows(db, "SELECT * FROM prep_jobs ORDER BY return_id,revision_sequence,job_id")


def _review_required(db):
    return {
        (row["firm_id"], row["return_id"]) for row in _rows(
            db, "SELECT firm_id, return_id FROM prep_returns WHERE review_required"
        )
    }


class Rollback(Exception):
    pass


def test_committed_file_can_dispatch_while_another_client_in_the_drop_is_missing(setup_dispatch, coordinator_db):
    dispatch, source = setup_dispatch
    upstream = source.upstream
    slots = (_slot("wages", "a"), _slot("ledger", "b"))
    with pytest.raises(Rollback), upstream.writer() as writer:
        writer.declare_batch(FIRM, "drop", slots, approved_grants=(GRANT,))
        rolled_back = _put(writer, "drop", "wages")
        assert upstream.feed() == ()
        assert dispatch.consume(rolled_back) is None
        raise Rollback
    assert upstream.feed() == ()
    assert dispatch.consume(rolled_back) is None

    with upstream.writer() as writer:
        writer.declare_batch(FIRM, "drop", slots, approved_grants=(GRANT,))
        wages = _put(writer, "drop", "wages")
    receipt = dispatch.consume(wages)
    assert receipt and _review_required(coordinator_db) == {(FIRM, "a")}
    restarted = UploadDispatch(coordinator_db, source)
    assert restarted.pending_extractions(limit=1) == (wages,)
    wages_ticket = _complete(restarted, wages, "a")
    (a,) = _jobs(coordinator_db)
    assert a["return_id"] == "a"
    assert [entry["version_id"] for entry in a["snapshot"]["inputs"]] == [wages.version_id]

    with upstream.writer() as writer:
        ledger = _put(writer, "drop", "ledger")
        assert dispatch.consume(ledger) is None
    assert dispatch.consume(ledger) not in {None, receipt}
    _complete(dispatch, ledger, "b")
    assert dispatch.consume(wages) == receipt
    assert not dispatch.stage_extraction(wages_ticket, PreparedExtraction("a", lambda _: "replayed-facts"))
    assert dispatch.begin_extraction(wages, GRANT, POLICY) == "no-work"

    unchanged, b = _jobs(coordinator_db)
    assert unchanged == a and b["return_id"] == "b"
    assert [entry["version_id"] for entry in b["snapshot"]["inputs"]] == [ledger.version_id]
    assert {row["job_id"] for row in _rows(coordinator_db, "SELECT job_id FROM prep_job_outbox")} == {
        a["job_id"], b["job_id"],
    }


def test_verification_cannot_swap_the_committed_original(setup_dispatch, coordinator_db, monkeypatch):
    dispatch, source = setup_dispatch
    first, second = _commit(source.upstream, "drop", _slot("wages", "a"), _slot("ledger", "b"))
    original = source.verify(first)
    assert dispatch.consume(first) == original.receipt_id
    with monkeypatch.context() as patch:
        patch.setattr(source, "verify", lambda _: original)
        with pytest.raises(ValueError, match="does not match the upload event"):
            dispatch.consume(second)
    assert dispatch.pending_extractions() == (first,)
    assert _review_required(coordinator_db) == {(FIRM, "a")}
    assert dispatch.consume(second) == source.verify(second).receipt_id


def test_no_authorization_keeps_the_review_notice_and_a_replay_does_not_restore_signoff_flag(
    setup_dispatch, coordinator_db
):
    dispatch, source = setup_dispatch
    event, unknown = _commit(source.upstream, "no-grant", _slot("wages", "a", grant=None), _slot("ledger", grant=None))
    assert dispatch.consume(event)
    assert dispatch.consume(unknown)
    assert _review_required(coordinator_db) == {(FIRM, "a")}
    assert dispatch.begin_extraction(event, None, POLICY) == "denied"
    assert dispatch.begin_extraction(event, GRANT, POLICY) == "no-work"
    assert not dispatch.finish_without_extraction(event)
    assert dispatch.finish_without_extraction(unknown, return_id="b")
    assert _review_required(coordinator_db) == {(FIRM, "a"), (FIRM, "b")}
    assert _jobs(coordinator_db) == []
    with coordinator_db.transaction() as conn:
        conn.execute("UPDATE prep_returns SET review_required = false WHERE (firm_id, return_id) = (%s, 'a')", (FIRM,))
    assert dispatch.consume(event)
    assert _review_required(coordinator_db) == {(FIRM, "b")}


@pytest.mark.parametrize("mismatch", ["firm", "version", "expiry", "class", "receipt"])
def test_invalid_current_grant_does_not_leave_a_claimable_receipt(setup_dispatch, coordinator_db, monkeypatch, mismatch):
    dispatch, source = setup_dispatch
    (event,) = _commit(source.upstream, "approved", _slot("wages", "a"))
    receipt_id = dispatch.consume(event)
    grant = source.verify_grant(event, GRANT)
    assert grant.receipt_id == receipt_id
    denied = {
        "firm": replace(grant, event=replace(event, firm_id=FOREIGN)),
        "version": replace(grant, event=replace(event, version_id="different-version")),
        "expiry": replace(grant, expires_at=datetime.now(UTC) - timedelta(minutes=1)),
        "class": replace(grant, policy=replace(POLICY, data_class="approved_redacted")),
        "receipt": replace(grant, receipt_id="different-original"),
    }[mismatch]
    monkeypatch.setattr(source, "verify_grant", lambda *_: denied)
    assert dispatch.begin_extraction(event, GRANT, POLICY) == "denied"
    assert dispatch.pending_extractions() == ()
    assert dispatch.begin_extraction(event, GRANT, POLICY) == "no-work"
    assert _review_required(coordinator_db) == {(FIRM, "a")}
    assert _rows(coordinator_db, "SELECT state FROM prep_dispatch_uploads") == [{"state": "no_authorization"}]


def test_nonmatching_worker_leaves_grant_available_and_terminal_attempt_is_not_replayed(setup_dispatch, coordinator_db):
    dispatch, source = setup_dispatch
    (event,) = _commit(source.upstream, "approved", _slot("wages", "a"))
    assert dispatch.consume(event)
    for policy in (
        replace(POLICY, provider_id="other-provider"),
        replace(POLICY, account_id="other-account"),
        replace(POLICY, model_id="other-model"),
        replace(POLICY, endpoint="https://other.example.test"),
        replace(POLICY, data_class="approved_redacted"),
        replace(POLICY, data_class="live"),
    ):
        assert dispatch.begin_extraction(event, GRANT, policy) == "no-work"
        assert dispatch.pending_extractions(firm_id=FIRM) == (event,)
        assert dispatch.pending_extractions(firm_id=FOREIGN) == ()
    ticket = _claim(dispatch, event)
    assert ticket.sha256 == sha256(source.upstream.read_version(FIRM, event.version_id)).hexdigest()
    assert ticket.return_id == "a"
    restarted = UploadDispatch(coordinator_db, source)
    assert restarted.pending_extractions() == ()
    assert restarted.begin_extraction(event, GRANT, POLICY) == "no-work"
    assert restarted.authorize_disclosure(ticket) == "no-work"
    assert not restarted.interrupt_extraction(replace(ticket, attempt_id="unrecognized"))
    assert restarted.interrupt_extraction(ticket)
    assert restarted.begin_extraction(event, GRANT, POLICY) == "no-work"
    assert not restarted.stage_extraction(ticket, PreparedExtraction("a", lambda _: "late-result"))
    assert _jobs(coordinator_db) == []
    assert _rows(coordinator_db, "SELECT state, grant_id FROM prep_dispatch_uploads") == [
        {"state": "interrupted", "grant_id": GRANT}
    ]


def test_unassigned_file_does_not_hold_up_a_return_and_later_version_links_to_predecessor(
    setup_dispatch, coordinator_db
):
    dispatch, source = setup_dispatch
    wages, later_assignment = _commit(source.upstream, "mix", _slot("wages", "a"), _slot("ledger"))
    assert dispatch.consume(wages)
    _complete(dispatch, wages, "a")
    assert [job["return_id"] for job in _jobs(coordinator_db)] == ["a"]
    assert dispatch.consume(later_assignment)
    assert _review_required(coordinator_db) == {(FIRM, "a")}
    _complete(dispatch, later_assignment, "b")
    assert _review_required(coordinator_db) == {(FIRM, "a"), (FIRM, "b")}
    initial = _jobs(coordinator_db)
    assert [job["snapshot"]["inputs"][0]["version_id"] for job in initial] == [wages.version_id, later_assignment.version_id]
    assert initial[0]["snapshot"]["inputs"][0]["extraction_policy"] == {
        "provider_id": POLICY.provider_id, "account_id": POLICY.account_id,
        "model_id": POLICY.model_id, "data_class": POLICY.data_class,
        "endpoint": POLICY.endpoint,
    }

    (correction,) = _commit(source.upstream, "correction", _slot("wages", "a"))
    assert correction.upload_id == wages.upload_id and correction.version_id != wages.version_id
    assert dispatch.consume(correction)
    _complete(dispatch, correction, "a")
    revisions = {job["input_revision"]: job["snapshot"] for job in _jobs(coordinator_db) if job["return_id"] == "a"}
    assert len(revisions) == 2
    assert revisions[initial[0]["input_revision"]] == initial[0]["snapshot"]
    successor = next(snapshot for revision, snapshot in revisions.items() if revision != initial[0]["input_revision"])
    assert successor["parent_revision"] == initial[0]["input_revision"]
    assert {r["inputs"][0]["version_id"] for r in revisions.values()} == {wages.version_id, correction.version_id}


def test_wrong_assignments_and_firms_cannot_enqueue_a_writer(setup_dispatch, coordinator_db):
    dispatch, source = setup_dispatch
    wages, wrong_result, unrelated, cross_firm = _commit(
        source.upstream, "invalid-scopes", _slot("wages", "a"), _slot("unrelated-return", "a"),
        _slot("independent", "b"), _slot("foreign-scope", "foreign"),
    )
    assert dispatch.consume(replace(cross_firm, firm_id=FOREIGN)) is None
    for event in (wages, wrong_result, unrelated, cross_firm):
        assert dispatch.consume(event)
    assert dispatch.begin_extraction(cross_firm, GRANT, POLICY) == "no-work"
    _complete(dispatch, wages, "a")
    _complete(dispatch, wrong_result, "b")
    (job_a,) = _jobs(coordinator_db)
    assert job_a["return_id"] == "a"
    assert [value["version_id"] for value in job_a["snapshot"]["inputs"]] == [wages.version_id]
    _complete(dispatch, unrelated, "b")
    _, job_b = _jobs(coordinator_db)
    assert (job_b["firm_id"], job_b["return_id"]) == (FIRM, "b")
    assert [value["version_id"] for value in job_b["snapshot"]["inputs"]] == [unrelated.version_id]
    assert _review_required(coordinator_db) == {(FIRM, "a"), (FIRM, "b")}
    assert {row["upload_id"] for row in _rows(
        coordinator_db, "SELECT upload_id FROM prep_dispatch_uploads WHERE state = 'misassigned'"
    )} == {wrong_result.upload_id, cross_firm.upload_id}


def test_concurrent_delivery_and_claim_dedup_by_committed_version(setup_dispatch, coordinator_db):
    dispatch, source = setup_dispatch
    (event,) = _commit(source.upstream, "concurrent", _slot("wages", "a"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(dispatch.consume, [event] * 8))
        assert len(set(receipts)) == 1 and receipts[0] is not None
        claims = list(pool.map(lambda _: dispatch.begin_extraction(event, GRANT, POLICY), range(8)))
        (ticket,) = [claim for claim in claims if isinstance(claim, ExtractionTicket)]
        decisions = list(pool.map(lambda _: dispatch.authorize_disclosure(ticket), range(8)))
        assert decisions.count("authorized") == 1
        results = list(pool.map(
            lambda _: dispatch.stage_extraction(ticket, PreparedExtraction("a", lambda _: "facts")), range(8),
        ))
    assert results.count(True) == 1
    assert dispatch.settle_staged(event)
    assert not dispatch.settle_staged(event)
    assert len(_jobs(coordinator_db)) == 1
    assert len(_rows(coordinator_db, "SELECT job_id FROM prep_job_outbox")) == 1


def test_artifact_commit_is_atomic_and_outbox_recovery_needs_no_old_ticket(setup_dispatch, coordinator_db):
    dispatch, source = setup_dispatch
    a, b = _commit(source.upstream, "broker", _slot("wages", "a"), _slot("ledger", "b"))
    assert dispatch.consume(a) and dispatch.consume(b)
    ticket = _claim(dispatch, a)

    def persist(conn):
        conn.execute("INSERT INTO test_facts VALUES ('durable-facts')")
        return "durable-facts"

    with coordinator_db.transaction() as conn:
        conn.execute("CREATE TABLE test_facts (artifact_id text PRIMARY KEY)")
        conn.execute("ALTER TABLE prep_dispatch_uploads ADD CONSTRAINT test_reject_stage CHECK (staged_state IS NULL)")
    prepared = PreparedExtraction("a", persist)
    with pytest.raises(CheckViolation):
        dispatch.stage_extraction(ticket, prepared)
    assert _rows(coordinator_db, "SELECT * FROM test_facts") == []
    with coordinator_db.transaction() as conn:
        conn.execute("ALTER TABLE prep_dispatch_uploads DROP CONSTRAINT test_reject_stage")
        conn.execute("ALTER TABLE prep_job_outbox ADD CONSTRAINT test_reject_outbox CHECK (false)")
    assert dispatch.stage_extraction(ticket, prepared)
    with pytest.raises(CheckViolation):
        dispatch.settle_staged(a)
    assert _jobs(coordinator_db) == []
    assert _rows(coordinator_db, "SELECT job_id FROM prep_job_outbox") == []
    assert _rows(coordinator_db, "SELECT artifact_id,staged_state FROM prep_dispatch_uploads WHERE upload_id='wages'") == [
        {"artifact_id": "durable-facts", "staged_state": "accepted"},
    ]
    assert _rows(coordinator_db, "SELECT * FROM test_facts") == [{"artifact_id": "durable-facts"}]
    with coordinator_db.transaction() as conn:
        conn.execute("ALTER TABLE prep_job_outbox DROP CONSTRAINT test_reject_outbox")
        # An already durable artifact remains recoverable even after expiry.
        conn.execute("UPDATE prep_dispatch_uploads SET extraction_deadline=clock_timestamp()-interval '1 second'")
    restarted = UploadDispatch(coordinator_db, source)
    assert restarted.begin_extraction(a, GRANT, POLICY) == "no-work"
    assert restarted.recover_extractions(firm_id=FOREIGN) == 0
    assert restarted.recover_extractions(firm_id=FIRM) == 1
    assert restarted.recover_extractions(firm_id=FIRM) == 0
    assert _jobs(coordinator_db)[0]["snapshot"]["inputs"][0]["artifact_id"] == "durable-facts"
    _complete(dispatch, b, "b")

    class Broker:
        def __init__(self):
            self.calls = []
            self.delivered = {}

        def publish(self, job, *, idempotency_key):
            # Model a durable broker accept; a second call with the same key is a no-op.
            committed = _rows(coordinator_db, """SELECT job_id FROM prep_job_outbox
                              JOIN prep_jobs USING (firm_id, job_id) WHERE (firm_id, job_id) = (%s, %s)""",
                              (job.firm_id, job.job_id))
            self.calls.append((idempotency_key, job, committed))
            self.delivered.setdefault(idempotency_key, job)
            if len(self.calls) == 1:
                raise ConnectionError("first acknowledgement lost after delivery")

    broker = Broker()
    assert dispatch.publish_pending(broker, limit=1) == PublicationResult(1, 0)
    restarted = UploadDispatch(coordinator_db, source)
    assert restarted.publish_pending(broker, limit=1) == PublicationResult(1, 1)
    assert restarted.publish_pending(broker, limit=1) == PublicationResult(1, 1)
    assert restarted.publish_pending(broker, limit=1) == PublicationResult(0, 0)
    first, second, retry = broker.calls
    assert first == retry and first != second and len(broker.delivered) == 2
    for key, job, committed in broker.calls:
        assert isinstance(job, QueuedJob)
        assert (key, job.firm_id, committed) == (job.job_id, FIRM, [{"job_id": job.job_id}])


def test_restart_interrupts_expired_attempt_and_fences_result_even_before_recovery(setup_dispatch, coordinator_db):
    dispatch, source = setup_dispatch
    slow, same_return, independent = _commit(
        source.upstream, "expired", _slot("slow", "a"), _slot("same", "a"), _slot("other", "b"),
    )
    for event in (slow, same_return, independent):
        assert dispatch.consume(event)
    ticket = _claim(dispatch, slow)
    _complete(dispatch, same_return, "a")
    _complete(dispatch, independent, "b")
    assert [job["return_id"] for job in _jobs(coordinator_db)] == ["b"]
    restarted = UploadDispatch(coordinator_db, source)
    assert restarted.recover_extractions() == 0
    with coordinator_db.transaction() as conn:
        conn.execute("""UPDATE prep_dispatch_uploads SET extraction_deadline=clock_timestamp()-interval '1 second'
                        WHERE (firm_id,upload_id,version_id)=(%s,%s,%s)""",
                     (slow.firm_id, slow.upload_id, slow.version_id))

    def never_persist(_):
        pytest.fail("A late response must not write any artifact")

    prepared = PreparedExtraction("a", never_persist)
    assert not dispatch.stage_extraction(ticket, prepared)
    assert restarted.recover_extractions() == 1
    assert restarted.begin_extraction(slow, GRANT, POLICY) == "no-work"
    assert not dispatch.stage_extraction(ticket, prepared)
    a, b = _jobs(coordinator_db)
    assert (a["return_id"], b["return_id"]) == ("a", "b")
    assert [entry["version_id"] for entry in a["snapshot"]["inputs"]] == [same_return.version_id]
    assert _rows(coordinator_db, "SELECT state FROM prep_dispatch_uploads WHERE upload_id='slow'") == [
        {"state": "interrupted"},
    ]


def test_revision_sequence_and_pinned_reasoning_are_independent_of_wall_clock(setup_dispatch, coordinator_db):
    _, source = setup_dispatch
    policy = StagePolicy(
        ProviderIdentity("openai", "reasoning-only", "https://reasoning.example.test", "reasoning-account"),
        "reasoning", frozenset({"synthetic"}), 7, 8192, 15, 512, 128,
        Decimal("1.25"), Decimal("2.75"), Decimal("0.10"),
    )
    dispatch = UploadDispatch(coordinator_db, source, reasoning_policy=policy)
    for number in range(3):
        (event,) = _commit(source.upstream, f"revision-{number}", _slot("wages", "a"))
        assert dispatch.consume(event)
        _complete(dispatch, event, "a")
        if number == 0:
            with coordinator_db.transaction() as conn:
                conn.execute("UPDATE prep_jobs SET created_at=clock_timestamp()+interval '2 days'")
    first, second, third = _jobs(coordinator_db)
    assert first["created_at"] > third["created_at"]
    assert [row["revision_sequence"] for row in (first, second, third)] == [1, 2, 3]
    assert [row["snapshot"]["parent_revision"] for row in (first, second, third)] == [
        None, first["input_revision"], second["input_revision"],
    ]
    assert all(row["snapshot"]["reasoning_policy"] == policy.to_wire() for row in (first, second, third))