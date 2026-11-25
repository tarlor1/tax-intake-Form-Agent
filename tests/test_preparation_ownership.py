import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest
from psycopg.types.json import Jsonb

from src.preparation.ownership import OwnerGrant, OwnershipBlocked, OwnershipJournal, OwnershipStore, RecoveryEvidence, _SCHEMA
from src.preparation.tool_contract import (
    Action, ActionOutcome, Choice, MutationDetails, Observation, Verification, VerifiedReadback,
)
from src.sigma_demo.native import PracticeAuthority


FIRM = "practice-firm"
PROFILE = "sigma-practice"
STORE = "store-2025"
YEAR = 2025


@pytest.fixture
def ownership(coordinator_db):
    native = PracticeAuthority()
    store = OwnershipStore(coordinator_db, native_authority=native)
    store.install()
    return store, native


def enroll(store, machine, *, shared=True, profile=PROFILE, year=YEAR, store_id=STORE, available=True):
    store.register_machine(FIRM, machine, "worker-" + machine)
    store.qualify_machine(FIRM, machine, profile, year, store_id, shared_store=shared, checkout_mode="native")
    if available:
        store.set_available(FIRM, machine, True)


def add_return(store, rid, *, draft=True, pinned=None):
    with store.db.transaction() as db:
        db.execute("""INSERT INTO prep_returns (firm_id, return_id, profile_id, store_id, tax_year,
                      draft_key, pinned_machine_id) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                   (FIRM, rid, PROFILE, STORE, YEAR, "draft-" + rid if draft else None, pinned))


def add_job(store, rid, job_id, *, sequence=0, parent=None, created_offset_seconds=0):
    snapshot = {"facts": [job_id]}
    if parent is not None:
        snapshot["parent_revision"] = parent
    with store.db.transaction() as db:
        db.execute("""INSERT INTO prep_jobs (firm_id, job_id, return_id, input_revision, profile_id, snapshot,
                      revision_sequence, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s,
                      clock_timestamp() + %s::integer * interval '1 second')""",
                   (FIRM, job_id, rid, job_id, PROFILE, Jsonb(snapshot), sequence, created_offset_seconds))


def claim(store, job_id, machine):
    return store.claim_worker(FIRM, job_id, machine, "worker-" + machine)


def get_job(store, job_id):
    with store.db.transaction() as db:
        return db.execute("SELECT state, snapshot FROM prep_jobs WHERE firm_id = %s AND job_id = %s",
                          (FIRM, job_id)).fetchone()


def get_steps(store, job_id):
    with store.db.transaction() as db:
        return db.execute("SELECT * FROM prep_steps WHERE firm_id = %s AND job_id = %s ORDER BY step_order",
                          (FIRM, job_id)).fetchall()


def activate(store, native, grant, draft_key):
    native.checkouts.add((grant, draft_key, "checkout"))
    store.activate(grant, "checkout")


def release(store, native, grant, draft_key, **kwargs):
    native.closes.add((grant, draft_key, "closed"))
    store.release(grant, "closed", **kwargs)


def recover(store, native, grant, draft_key, evidence):
    native.reopens[(grant, draft_key, "independent-reopen")] = evidence
    return store.reconcile(grant, "independent-reopen")


def choice(observation="obs-1", control="ref-1", *, before=None, intended="0.00"):
    return Choice(action=Action(name="set_field", arguments={
        "observationId": observation, "controlRef": control, "bindingId": "box-2", "factIds": ["fact-2"],
    }), semantic_key="w2.withholding", mutation=MutationDetails(before=before, intended=intended))


def race(left, right):
    barrier = Barrier(2)

    def run(command):
        barrier.wait(timeout=10)
        return command()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(run, command) for command in (left, right)]
        return [result.result(timeout=10) for result in results]


def test_two_clients_serialize_returns_and_parallelize_unrelated_sessions(ownership):
    store, native = ownership
    other = OwnershipStore(store.db, native_authority=native)
    for machine in ("a", "b", "c", "d"):
        enroll(store, machine)
    for rid in ("one", "two", "three", "four"):
        add_return(store, rid)
        add_job(store, rid, rid)
    add_job(store, "one", "successor")

    same_return = race(lambda: claim(store, "one", "a"), lambda: claim(other, "successor", "b"))
    assert same_return[0].job_id == "one" and same_return[1] is None
    assert claim(store, "four", next(grant.machine_id for grant in same_return if grant)) is None
    assert get_job(store, "four")["state"] == "queued"
    unrelated = race(lambda: claim(store, "two", "c"), lambda: claim(other, "three", "d"))
    assert [grant.return_id for grant in unrelated] == ["two", "three"]
    assert [grant.generation for grant in unrelated] == [1, 1]


def test_worker_eligibility_checks_scope_explicit_availability_and_pinning(ownership):
    store, _ = ownership
    add_return(store, "one", pinned="assigned")
    add_job(store, "one", "job")
    enroll(store, "other")
    enroll(store, "assigned", shared=False, available=False)
    assert claim(store, "job", "other") is None
    assert claim(store, "job", "assigned") is None
    store.set_available(FIRM, "assigned", True)
    assert store.claim_worker(FIRM, "job", "assigned", "not-the-worker") is None
    assert claim(store, "job", "assigned").machine_id == "assigned"

    add_return(store, "two")
    add_job(store, "two", "job-2")
    for machine, capabilities in (
        ("wrong-profile", {"profile": "wrong"}), ("wrong-year", {"year": 2024}),
        ("wrong-store", {"store_id": "unconnected"}), ("unshared", {"shared": False}),
    ):
        enroll(store, machine, **capabilities)
        assert claim(store, "job-2", machine) is None
    assert store.claim_worker("other-firm", "job-2", "other", "worker-other") is None
    assert claim(store, "job-2", "other") is not None


def test_worker_order_uses_frozen_sequence_and_nonterminal_predecessor_state(ownership):
    store, native = ownership
    enroll(store, "older")
    enroll(store, "newer")
    add_return(store, "one")
    add_job(store, "one", "new-input", sequence=2, parent="original", created_offset_seconds=-60)
    assert claim(store, "new-input", "newer") is None  # The recorded parent does not exist yet.
    add_job(store, "one", "original", sequence=1)
    for state in ("preparing", "verifying", "reconciling", "queued"):
        # Model an incomplete job record without an active owner row. Recovery
        # must be based on the stored state, not merely the existence of a lock.
        with store.db.transaction() as db:
            db.execute("UPDATE prep_jobs SET state = %s WHERE job_id = 'original'", (state,))
        assert claim(store, "new-input", "newer") is None
    original = claim(store, "original", "older")
    activate(store, native, original, "draft-one")
    release(store, native, original, "draft-one", issue="missing-client-input")
    assert claim(store, "new-input", "newer").job_id == "new-input"


@pytest.mark.parametrize("parent", ["missing", "first", None])
def test_worker_requires_the_exact_immediate_predecessor(ownership, parent):
    store, native = ownership
    enroll(store, "worker")
    add_return(store, "one")
    add_job(store, "one", "first", sequence=1)
    add_job(store, "one", "second", sequence=2, parent="first")
    add_job(store, "one", "third", sequence=3, parent=parent)
    for job in ("first", "second"):
        owner = claim(store, job, "worker")
        activate(store, native, owner, "draft-one")
        release(store, native, owner, "draft-one", issue="missing-client-input")
    assert claim(store, "third", "worker") is None


def test_reviews_before_first_draft_freeze_exact_inputs_without_blocking_preparation(ownership):
    store, native = ownership
    enroll(store, "writer")
    enroll(store, "reviewer", available=False)
    add_return(store, "one", draft=False)
    add_return(store, "other")
    add_job(store, "other", "outside")
    add_job(store, "one", "first", sequence=1)
    add_job(store, "one", "successor", sequence=2, parent="first")
    with pytest.raises(OwnershipBlocked, match="review-revision-unavailable"):
        store.request_review(FIRM, "one", "reviewer", "alice", input_revision="outside")
    later = store.request_review(FIRM, "one", "reviewer", "alice", input_revision="successor")
    initial = store.request_review(FIRM, "one", "reviewer", "alice")
    assert initial != later
    assert store.request_review(FIRM, "one", "reviewer", "alice", input_revision="first") == initial
    assert store.find_review_request(FIRM, "one", "alice", "first") == initial
    assert store.find_review_request(FIRM, "one", "alice", "successor") == later
    assert store.find_review_request(FIRM, "other", "alice", "first") is None
    assert store.find_review_request("other-firm", "one", "alice", "first") is None
    assert store.find_review_request(FIRM, "one", "somebody-else", "first") is None
    assert store.claim_review(FIRM, "one", initial, "alice") is None
    first = claim(store, "first", "writer")
    activate(store, native, first, None)
    native.absent.add((first, "search"))
    create = store.begin_create(first, "search")
    native.created[first, create, "created"] = "bound"
    store.complete_create(first, create, "created")
    release(store, native, first, "bound", issue="missing-client-input")

    # The oldest request is for a later revision; the ready original still gets
    # priority over the successor's writer. Recreating the store models restart.
    store = OwnershipStore(store.db, native_authority=native)
    assert store.request_review(FIRM, "one", "reviewer", "alice") == initial
    assert store.claim_review(FIRM, "one", later, "alice") is None
    assert claim(store, "successor", "writer") is None
    review = store.claim_review(FIRM, "one", initial, "alice")
    assert store.review_input_revision(review) == "first"
    release(store, native, review, "bound")
    assert store.find_review_request(FIRM, "one", "alice", "first") is None
    with pytest.raises(OwnershipBlocked, match="stale-owner"):
        store.review_input_revision(review)

    successor = claim(store, "successor", "writer")
    assert successor.generation > review.generation
    with pytest.raises(OwnershipBlocked, match="review-revision-superseded"):
        store.request_review(FIRM, "one", "reviewer", "alice", input_revision="first")
    release(store, native, successor, "bound", issue="missing-client-input")
    later_review = store.claim_review(FIRM, "one", later, "alice")
    assert store.review_input_revision(later_review) == "successor"


def test_prebinding_review_on_unqualified_native_host_does_not_block_successor(ownership):
    store, native = ownership
    enroll(store, "writer", shared=False)
    enroll(store, "reviewer")
    add_return(store, "one", draft=False)
    add_job(store, "one", "first", sequence=1)
    add_job(store, "one", "successor", sequence=2, parent="first")
    request = store.request_review(FIRM, "one", "reviewer", "alice", input_revision="first")
    first = claim(store, "first", "writer")
    activate(store, native, first, None)
    native.existing[first, "found"] = "pinned-draft"
    store.bind_existing_draft(first, "found")
    release(store, native, first, "pinned-draft", issue="missing-client-input")
    assert store.claim_review(FIRM, "one", request, "alice") is None
    assert claim(store, "successor", "writer") is not None


def test_pending_review_survives_worker_and_holds_return_and_machine_until_native_close(ownership):
    store, native = ownership
    for machine in ("writer", "reviewer", "spare"):
        enroll(store, machine)
    add_return(store, "one")
    add_return(store, "two")
    add_job(store, "one", "original")
    add_job(store, "one", "new-input")
    add_job(store, "two", "independent")
    old = claim(store, "original", "writer")
    activate(store, native, old, "draft-one")
    request = store.request_review(FIRM, "one", "reviewer", "alice")
    assert store.request_review(FIRM, "one", "reviewer", "alice") == request
    assert store.claim_review(FIRM, "one", request, "alice") is None
    assert store.scope(old).owner_generation == old.generation
    release(store, native, old, "draft-one", issue="missing-client-input")

    next_worker, review = race(lambda: claim(store, "new-input", "spare"),
                               lambda: store.claim_review(FIRM, "one", request, "alice"))
    assert next_worker is None and review.generation == old.generation + 1
    assert store.review_input_revision(review) == "original"
    with store.db.transaction() as db, pytest.raises(OwnershipBlocked, match="checkout-unproven"):
        store.require_review_checkout(db, review)
    activate(store, native, review, "draft-one")
    with store.db.transaction() as db:
        store.require_review_checkout(db, review)
    with pytest.raises(OwnershipBlocked, match="review-session-in-use"):
        store.set_available(FIRM, "reviewer", True)
    assert claim(store, "independent", "reviewer") is None
    assert claim(store, "independent", "spare") is not None
    with pytest.raises(OwnershipBlocked, match="native-close-unproven"):
        store.release(review, "evidence-tab-closed")
    with pytest.raises(OwnershipBlocked, match="native-close-unproven"):
        store.release(review, "closed")  # Same opaque proof existed for the old worker only.
    assert claim(store, "new-input", "writer") is None
    release(store, native, review, "draft-one")
    assert claim(store, "new-input", "reviewer") is None  # Requires explicit release for reuse.
    assert claim(store, "new-input", "writer").generation == review.generation + 1


def test_interrupted_review_requires_qualified_independent_exact_close(ownership):
    store, native = ownership
    for machine in ("reviewer", "writer"):
        enroll(store, machine)
    add_return(store, "one")
    add_job(store, "one", "first", sequence=1)
    add_job(store, "one", "successor", sequence=2, parent="first")
    first = claim(store, "first", "writer")
    release(store, native, first, "draft-one", issue="missing-client-input")
    request = store.request_review(FIRM, "one", "reviewer", "alice", input_revision="first")
    review = store.claim_review(FIRM, "one", request, "alice")
    activate(store, native, review, "draft-one")
    store.interrupt(review, "disconnected")
    for evidence in (RecoveryEvidence("different-draft", True, True), RecoveryEvidence("draft-one", False, True)):
        with pytest.raises(OwnershipBlocked, match="native-close-unproven"):
            recover(store, native, review, "draft-one", evidence)
        assert claim(store, "successor", "writer") is None
    with store.db.transaction() as db:
        db.execute("DELETE FROM prep_machine_profiles WHERE machine_id = 'reviewer'")
    with pytest.raises(OwnershipBlocked, match="native-close-unproven"):
        recover(store, native, review, "draft-one", RecoveryEvidence("draft-one", True, True))
    store.qualify_machine(FIRM, "reviewer", PROFILE, YEAR, STORE, shared_store=True, checkout_mode="native")
    assert recover(store, native, review, "draft-one", RecoveryEvidence("draft-one", True, False)) == "exact"
    assert claim(store, "successor", "writer") is not None


def test_agent_permits_commit_intent_readback_is_separate_and_revisions_stay_frozen(ownership):
    store, native = ownership
    enroll(store, "a")
    add_return(store, "one")
    add_job(store, "one", "job")
    grant = claim(store, "job", "a")
    with pytest.raises(OwnershipBlocked, match="checkout-unproven"):
        store.activate(grant, "window-is-visible")
    activate(store, native, grant, "draft-one")
    scope = store.scope(grant)
    journal = OwnershipJournal(store, grant)

    async def mutate():
        await journal.require_owner(scope)
        with pytest.raises(OwnershipBlocked, match="job-scope-mismatch"):
            await journal.require_owner(scope.model_copy(update={"draft_key": "other-draft"}))
        permit = await journal.begin(scope, choice())
        step, = get_steps(store, "job")
        assert (step["step_id"], step["generation"], step["draft_key"], step["state"]) == (
            permit.step_id, grant.generation, "draft-one", "pending",
        )
        assert step["operation"] == {"kind": "set_field", "target": "box-2"}
        assert step["expected_prior"] is None and step["intended"] == "0.00"
        with pytest.raises(OwnershipBlocked, match="pending-native-intent"):
            await journal.begin(scope, choice("obs-2", "ref-2"))
        with pytest.raises(OwnershipBlocked, match="unconfirmed-native-readback"):
            await journal.readback(permit, ActionOutcome(status="uncertain"), Observation(scope=scope, observation_id="obs-2"))
        await journal.readback(permit, ActionOutcome(status="applied"), Observation(scope=scope, observation_id="obs-2"))
        with pytest.raises(OwnershipBlocked, match="native-intent-already-recorded"):
            await journal.begin(scope, choice("obs-3", "new-control-ref"))

    asyncio.run(mutate())
    step, = get_steps(store, "job")
    assert step["state"] == "readback"
    assert step["immediate_readback"] == {"status": "applied", "observation_id": "obs-2"}
    assert get_job(store, "job")["state"] == "preparing"
    store.start_verification(grant)
    store.record_verification(grant, Verification(scope=scope, verified_binding_ids=("box-2",)))
    assert get_job(store, "job")["state"] == "verifying"
    release(store, native, grant, "draft-one")
    assert get_job(store, "job") == {"state": "ready_for_review", "snapshot": {"facts": ["job"]}}
    with pytest.raises(psycopg.errors.RaiseException, match="inputs are immutable"), store.db.transaction() as db:
        db.execute("UPDATE prep_jobs SET snapshot = '{}'::jsonb WHERE job_id = 'job'")
    with pytest.raises(psycopg.errors.RaiseException, match="inputs are immutable"), store.db.transaction() as db:
        db.execute("UPDATE prep_jobs SET revision_sequence = 2 WHERE job_id = 'job'")
    with pytest.raises(psycopg.errors.RaiseException, match="jobs are immutable"), store.db.transaction() as db:
        db.execute("DELETE FROM prep_jobs WHERE job_id = 'job'")


def test_reopened_native_values_and_evidence_projection_commit_together(ownership):
    store, native = ownership
    enroll(store, "a")
    add_return(store, "one")
    add_job(store, "one", "job", sequence=1)
    grant = claim(store, "job", "a")
    activate(store, native, grant, "draft-one")
    scope = store.scope(grant)
    store.start_verification(grant)
    readback = VerifiedReadback(scope=scope, section_key="w2", field_key="withholding", binding_id="box-2",
                                source_record_id="source-w2", native_row_key="native-w2", value_kind="decimal",
                                fact_ids=("source-box-2",),
                                normalized_value="0.00", matches_intended=True)
    verified = Verification(scope=scope, verified_binding_ids=("box-2",), readbacks=(readback,))

    def apply(connection, result):
        assert result.readbacks == (readback,)
        connection.execute("UPDATE prep_returns SET review_required = true WHERE return_id = 'one'")

    def failing_projection(connection, result):
        apply(connection, result)
        raise RuntimeError("projection failed")

    with pytest.raises(RuntimeError, match="projection failed"):
        store.record_verification(grant, verified, apply=failing_projection)
    with store.db.transaction() as db:
        assert db.execute("SELECT result FROM prep_verifications").fetchone() is None
        assert not db.execute("SELECT review_required FROM prep_returns").fetchone()["review_required"]
    journal = OwnershipJournal(store, grant, persist_verification=apply)
    asyncio.run(journal.record_verification(verified))
    with store.db.transaction() as db:
        assert db.execute("SELECT result FROM prep_verifications").fetchone()["result"] == verified.model_dump(mode="json")
        assert db.execute("SELECT review_required FROM prep_returns").fetchone()["review_required"]


@pytest.mark.parametrize("disconnected", [False, True])
def test_expiry_or_disconnect_cannot_transfer_pending_intent_and_exact_is_not_replayed(ownership, disconnected):
    store, native = ownership
    for machine in ("lost", "backup", "independent"):
        enroll(store, machine)
    add_return(store, "one")
    add_return(store, "two")
    add_job(store, "one", "job")
    add_job(store, "two", "other-job")
    old = claim(store, "job", "lost")
    activate(store, native, old, "draft-one")
    scope = store.scope(old)
    intent = store.begin(old, scope, choice())
    if disconnected:
        store.interrupt(old, "disconnected")
    else:
        with store.db.transaction() as db:
            db.execute("UPDATE prep_owners SET lease_until = clock_timestamp() - interval '1 second'")
        with pytest.raises(OwnershipBlocked, match="owner-not-dispatchable"):
            store.renew(old)
        assert claim(store, "job", "backup") is None
        store.interrupt(old, "expired")
    with pytest.raises(OwnershipBlocked, match="owner-not-dispatchable"):
        store.require_owner(old, scope)
    with pytest.raises(OwnershipBlocked, match="reconciliation-required"):
        release(store, native, old, "draft-one")
    assert claim(store, "job", "backup") is None
    assert get_job(store, "job")["state"] == "reconciling"
    assert claim(store, "other-job", "independent") is not None
    with pytest.raises(OwnershipBlocked, match="quiescence-or-native-release-unproven"):
        store.reconcile(old, "heartbeat-timed-out")
    assert claim(store, "job", "backup") is None

    assert recover(store, native, old, "draft-one", RecoveryEvidence("draft-one", True, True, "0.00")) == "exact"
    assert native.inspections[-1][2][0].step_id == intent.step_id
    step, = get_steps(store, "job")
    assert step["state"] == "reconciled_exact" and step["immediate_readback"] is None
    with pytest.raises(OwnershipBlocked, match="machine-unavailable"):
        store.set_available(FIRM, "lost", True)
    fresh = claim(store, "job", "backup")
    assert fresh.generation == old.generation + 1
    activate(store, native, fresh, "draft-one")
    checkpoint, = store.checkpoints(fresh)
    assert (checkpoint.step_id, checkpoint.state) == (intent.step_id, "reconciled_exact")
    with pytest.raises(OwnershipBlocked, match="native-intent-already-recorded"):
        store.begin(fresh, store.scope(fresh), choice("obs-4", "other-ref"))
    with pytest.raises(OwnershipBlocked, match="stale-owner"):
        store.require_owner(old, scope)


def test_proven_absence_with_unchanged_preconditions_permits_only_one_retry(ownership):
    store, native = ownership
    for machine in ("a", "b", "c"):
        enroll(store, machine)
    add_return(store, "one")
    add_job(store, "one", "job")
    after_absence = RecoveryEvidence("draft-one", True, True, None)
    for number, machine in enumerate(("a", "b"), 1):
        grant = claim(store, "job", machine)
        activate(store, native, grant, "draft-one")
        if number == 2:
            with pytest.raises(OwnershipBlocked, match="native-retry-preconditions-changed"):
                store.begin(grant, store.scope(grant), choice("obs-edited", "ref-edited", before="7.00"))
        store.begin(grant, store.scope(grant), choice(f"obs-{number}", f"ref-{number}"))
        store.interrupt(grant, "uncertain")
        assert recover(store, native, grant, "draft-one", after_absence) == "absent"
    assert [step["state"] for step in get_steps(store, "job")] == ["reconciled_absent", "reconciled_absent"]
    assert get_job(store, "job")["state"] == "needs_attention"
    assert claim(store, "job", "c") is None
    add_job(store, "one", "successor")
    assert claim(store, "successor", "c") is None


@pytest.mark.parametrize(("name", "args", "before", "intended"), [
    ("ensure_record", {"listRef": "list", "sourceRecordId": "expense-2"}, ["row-1"], ["row-1", "row-2"]),
    ("save_draft", {"scopeRef": "scope"}, {"status": "dirty"}, {"status": "saved"}),
])
def test_interrupted_add_or_save_is_checkpointed_and_not_dispatched_again(ownership, name, args, before, intended):
    store, native = ownership
    enroll(store, "a")
    enroll(store, "b")
    add_return(store, "one")
    add_job(store, "one", "job")

    def operation(obs, refs):
        return Choice(action=Action(name=name, arguments={"observationId": obs, **refs}),
                      semantic_key="expenses", mutation=MutationDetails(before=before, intended=intended))

    old = claim(store, "job", "a")
    activate(store, native, old, "draft-one")
    store.begin(old, store.scope(old), operation("obs-1", args))
    store.interrupt(old, "uncertain")
    assert recover(store, native, old, "draft-one", RecoveryEvidence("draft-one", True, True, intended)) == "exact"
    fresh = claim(store, "job", "b")
    activate(store, native, fresh, "draft-one")
    fresh_refs = {key: value if key == "sourceRecordId" else "new-" + value for key, value in args.items()}
    with pytest.raises(OwnershipBlocked, match="native-intent-already-recorded"):
        store.begin(fresh, store.scope(fresh), operation("obs-2", fresh_refs))


@pytest.mark.parametrize("evidence", [
    RecoveryEvidence("draft-one", False, True, None),
    RecoveryEvidence("draft-one", True, False, None),
    RecoveryEvidence("draft-one", True, True, False),  # Not numeric zero.
    RecoveryEvidence("other-draft", True, True, "0.00"),
])
def test_ambiguous_recovery_blocks_future_machine_writes_but_allows_known_draft_review(ownership, evidence):
    store, native = ownership
    enroll(store, "unsafe")
    enroll(store, "safe")
    add_return(store, "one")
    add_job(store, "one", "job")
    add_job(store, "one", "next")
    grant = claim(store, "job", "unsafe")
    activate(store, native, grant, "draft-one")
    store.begin(grant, store.scope(grant), choice(before=0))
    store.interrupt(grant, "unsafe")
    assert recover(store, native, grant, "draft-one", evidence) == "ambiguous"
    assert get_job(store, "job")["state"] == "needs_attention"
    assert claim(store, "next", "safe") is None
    review_id = store.request_review(FIRM, "one", "safe", "alice")
    assert store.claim_review(FIRM, "one", review_id, "alice") is not None


def test_existing_draft_binding_requires_exact_native_proof_and_audits_without_create(ownership):
    store, native = ownership
    enroll(store, "seat", shared=False)
    add_return(store, "one", draft=False)
    add_return(store, "other")
    add_job(store, "one", "job")
    grant = claim(store, "job", "seat")
    native.existing[grant, "exact-match"] = "durable-key"
    with pytest.raises(OwnershipBlocked, match="checkout-unproven"):
        store.bind_existing_draft(grant, "exact-match")
    activate(store, native, grant, None)
    with pytest.raises(OwnershipBlocked, match="existing-draft-unproven"):
        store.bind_existing_draft(grant, "worker-declared-match")
    native.existing[grant, "assigned-already"] = "draft-other"
    with pytest.raises(OwnershipBlocked, match="draft-already-assigned"):
        store.bind_existing_draft(grant, "assigned-already")
    assert store.bind_existing_draft(grant, "exact-match") == "durable-key"
    assert get_steps(store, "job") == []
    with store.db.transaction() as db:
        assert db.execute("""SELECT draft_key, pinned_machine_id FROM prep_returns
                             WHERE return_id = 'one'""").fetchone() == {
            "draft_key": "durable-key", "pinned_machine_id": "seat",
        }
        assert db.execute("""SELECT generation, draft_key, proof_id FROM prep_owner_events
                             WHERE event = 'existing-draft-bound'""").fetchall() == [
            {"generation": grant.generation, "draft_key": "durable-key", "proof_id": "exact-match"},
        ]
    with pytest.raises(OwnershipBlocked, match="checkout-unproven"):
        store.scope(grant)
    activate(store, native, grant, "durable-key")
    assert store.scope(grant).draft_key == "durable-key"
    with pytest.raises(OwnershipBlocked, match="existing-draft-binding-not-allowed"):
        store.bind_existing_draft(grant, "exact-match")


def test_create_is_journaled_after_complete_search_and_interrupted_create_binds_exact_key(ownership):
    store, native = ownership
    enroll(store, "a", shared=False)
    enroll(store, "b")
    add_return(store, "one", draft=False)
    add_job(store, "one", "job")
    grant = claim(store, "job", "a")
    activate(store, native, grant, None)
    with pytest.raises(OwnershipBlocked, match="create-not-authorized"):
        store.begin_create(grant, "name-matched-nothing")
    native.absent.add((grant, "unique-complete-search"))
    step_id = store.begin_create(grant, "unique-complete-search")
    native.existing[grant, "found-during-create"] = "durable-key"
    with pytest.raises(OwnershipBlocked, match="existing-draft-binding-not-allowed"):
        store.bind_existing_draft(grant, "found-during-create")
    with pytest.raises(OwnershipBlocked, match="invalid-draft-key"):
        store.complete_create(grant, step_id, "new-window-appeared")
    step, = get_steps(store, "job")
    assert step["operation"]["kind"] == "create" and step["draft_key"] is None and step["state"] == "pending"
    store.interrupt(grant, "disconnected")
    with pytest.raises(OwnershipBlocked, match="machine-still-owns-return"):
        store.restore_machine(FIRM, "a", "new-session-approved")
    evidence = RecoveryEvidence("durable-key", True, True, step["intended"])
    assert recover(store, native, grant, None, evidence) == "exact"
    with store.db.transaction() as db:
        bound = db.execute("SELECT draft_key, pinned_machine_id FROM prep_returns WHERE return_id = 'one'").fetchone()
    assert bound == {"draft_key": "durable-key", "pinned_machine_id": "a"}
    assert claim(store, "job", "b") is None
    assert get_steps(store, "job")[0]["state"] == "reconciled_exact"
    with pytest.raises(OwnershipBlocked, match="safe-session-unproven"):
        store.restore_machine(FIRM, "a", "heartbeat-returned")
    native.restorations.add((grant, "new-session-approved"))
    store.restore_machine(FIRM, "a", "new-session-approved")
    assert claim(store, "job", "a") is None
    store.set_available(FIRM, "a", True)
    fresh = claim(store, "job", "a")
    assert fresh.generation == grant.generation + 1
    activate(store, native, fresh, "durable-key")
    assert store.scope(fresh).draft_key == "durable-key"


def test_confirmed_create_and_verification_issue_require_correct_proof_and_close(ownership):
    store, native = ownership
    enroll(store, "a")
    add_return(store, "one", draft=False)
    add_job(store, "one", "job")
    grant = claim(store, "job", "a")
    activate(store, native, grant, None)
    native.absent.add((grant, "search"))
    step_id = store.begin_create(grant, "search")
    native.created[(grant, step_id, "verified-identity")] = "durable-key"
    store.complete_create(grant, step_id, "verified-identity")
    with pytest.raises(OwnershipBlocked, match="checkout-unproven"):
        store.scope(grant)  # Old checkout was bound to the pre-create identity only.
    activate(store, native, grant, "durable-key")
    scope = store.scope(grant)
    store.start_verification(grant)
    with pytest.raises(OwnershipBlocked, match="verification-scope-mismatch"):
        store.record_verification(grant, Verification(scope=scope.model_copy(update={"draft_key": "stale"})))
    store.record_verification(grant, Verification(scope=scope, issues=("missing-required-input",)))
    assert get_job(store, "job")["state"] == "verifying"
    release(store, native, grant, "durable-key")
    assert get_job(store, "job")["state"] == "needs_attention"


def test_ownership_upgrade_never_guesses_legacy_review_revision_or_releases_native_session(coordinator_db):
    native = PracticeAuthority()
    store = OwnershipStore(coordinator_db, native_authority=native)
    with coordinator_db.transaction() as db:
        db.execute(_SCHEMA)
    enroll(store, "seat")
    add_return(store, "one")
    add_job(store, "one", "first", sequence=1)
    with coordinator_db.transaction() as db:
        db.execute("""INSERT INTO prep_review_requests (firm_id, review_id, return_id, machine_id, reviewer_id, state)
                        VALUES (%s, 'old-pending', 'one', 'seat', 'alice', 'pending'),
                               (%s, 'old-owned', 'one', 'seat', 'bob', 'owned')""", (FIRM, FIRM))
        db.execute("""INSERT INTO prep_owners (firm_id, return_id, generation, machine_id, actor_id, review_id)
                      VALUES (%s, 'one', 1, 'seat', 'bob', 'old-owned')""", (FIRM,))
    store.install()
    with coordinator_db.transaction() as db:
        assert db.execute("""SELECT review_id, state, input_revision FROM prep_review_requests
                             ORDER BY request_order""").fetchall() == [
            {"review_id": "old-pending", "state": "cancelled", "input_revision": None},
            {"review_id": "old-owned", "state": "owned", "input_revision": None},
        ]
    owner = OwnerGrant(FIRM, "one", 1, "seat", "bob", None, "old-owned")
    assert claim(store, "first", "seat") is None
    with pytest.raises(OwnershipBlocked, match="review-revision-unavailable"):
        store.review_input_revision(owner)
    with pytest.raises(OwnershipBlocked, match="native-close-unproven"):
        store.release(owner, "old-session-window-gone")
    release(store, native, owner, "draft-one")
    assert claim(store, "first", "seat").generation == 2
