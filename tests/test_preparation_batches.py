import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
import json
from threading import Barrier, Event

import httpx
import pytest

from src.sigma_demo.sources import corporate_source, individual_sources
from sigma_practice_uploads import PracticeObjects
from src.preparation.agent import StagePolicy
from src.preparation.batches import BatchFile, BatchIntake, BatchManifest, ClassifiedUpload, IngestScope, CommittedBatchSource
from src.preparation.contracts import Amount, ExactClaim, OriginalRef, RecordIdentity
from src.preparation.dispatch import ExtractionPolicy, ExtractionTicket, PreparedExtraction, UploadCommitted, UploadDispatch
from src.preparation.evidence import OriginalScope, StoredOriginal, VerifiedFact, ground_occurrence, load_original
from src.preparation.providers import ProviderIdentity, open_provider
from src.preparation.upload_processing import UploadProcessor
from src.review_store import ReviewError


SCOPE = IngestScope("firm", "preparer", frozenset({"return-a"}))
EXTRACTION = ExtractionPolicy("gemini", "practice-account", "test-model", "synthetic", "https://example.test")


@pytest.fixture
def intake(coordinator_db, tmp_path):
    with coordinator_db.transaction() as db:
        db.execute("""INSERT INTO prep_returns (firm_id,return_id,profile_id,store_id,tax_year,draft_key)
                      VALUES ('firm','return-a','practice','store',2025,'draft-a')""")
    intake = BatchIntake(coordinator_db, PracticeObjects(tmp_path))
    intake.install()
    return intake


def manifest(batch_id, *documents, approved=False):
    return BatchManifest(batch_id=batch_id, approved_for_ai=approved, return_hint="return-a", files=tuple(
        BatchFile(slot=document.document_id, filename=document.filename, media_type=document.media_type)
        for document in documents
    ))


def upload(intake, batch_id, document):
    return intake.put(SCOPE, batch_id, document.document_id, document.media_type, document.content)


def test_committed_files_replay_independently_and_freeze_settled_inputs_per_return(intake, coordinator_db):
    documents = individual_sources()[:2]
    batch = manifest("first", *documents, approved=True)
    assert intake.declare(SCOPE, batch, policy=EXTRACTION).state == "incomplete"
    partial = upload(intake, "first", documents[0])
    assert partial.state == "incomplete" and len(intake.committed_events()) == 1
    assert upload(intake, "first", documents[0]) == partial
    with pytest.raises(ReviewError) as changed:
        intake.put(SCOPE, "first", documents[0].document_id, "application/pdf", documents[1].content)
    assert changed.value.status_code == 409
    assert upload(intake, "first", documents[1]).state == "sealed"
    assert intake.declare(SCOPE, batch, policy=EXTRACTION).state == "sealed"

    source = CommittedBatchSource(intake, lambda event, hint: ClassifiedUpload("return-a", "synthetic"))
    dispatch = UploadDispatch(coordinator_db, source)
    dispatch.install()
    events = intake.committed_events()
    assert len(events) == 2
    for event in events:
        assert dispatch.consume(event)
    assert intake.committed_events() == events

    restarted = UploadDispatch(coordinator_db, source)
    for event in events:
        assert restarted.consume(event)
        intake.record_publication(event, received=True)
    assert intake.committed_events() == ()
    assert set(restarted.pending_extractions()) == set(events)

    for event in events:
        grant_id = source.grant_id(event)
        assert restarted.begin_extraction(event, grant_id, ExtractionPolicy(
            "gemini", "practice-account", "test-model", "synthetic", "https://another-recipient.test",
        )) == "no-work"
        ticket = restarted.begin_extraction(event, grant_id, EXTRACTION)
        assert isinstance(ticket, ExtractionTicket)
        assert intake._objects.checksum(event.firm_id, ticket.object_id) == ticket.sha256
        assert restarted.authorize_disclosure(ticket) == "authorized"
        prepared = PreparedExtraction("return-a", lambda _: event.version_id)
        assert restarted.stage_extraction(ticket, prepared)
        assert not restarted.stage_extraction(ticket, prepared)
        assert restarted.settle_staged(event)

    with coordinator_db.transaction() as db:
        jobs = db.execute("SELECT snapshot FROM prep_jobs").fetchall()
        assert len(jobs) == 1 and len(jobs[0]["snapshot"]["inputs"]) == 2
        assert db.execute("SELECT count(*) AS n FROM prep_job_outbox").fetchone()["n"] == 1
        assert db.execute("SELECT review_required FROM prep_returns").fetchone()["review_required"]


def test_exclude_bad_slot_denied_processing_still_flags_assigned_return(intake, coordinator_db):
    valid, invalid = individual_sources()[:2]
    batch = manifest("first", valid, invalid)
    intake.declare(SCOPE, batch, policy=EXTRACTION)
    upload(intake, "first", valid)
    with pytest.raises(ReviewError) as malformed:
        intake.put(SCOPE, "first", invalid.document_id, "application/pdf", b"not a PDF")
    assert malformed.value.status_code == 422
    (event,) = intake.committed_events()
    with pytest.raises(ReviewError) as other_actor:
        intake.exclude(IngestScope("firm", "another-preparer", frozenset({"return-a"})), "first", invalid.document_id)
    assert other_actor.value.status_code == 404
    assert intake.exclude(SCOPE, "first", invalid.document_id).state == "sealed"

    assert intake.committed_events() == (event,)
    source = CommittedBatchSource(intake, lambda event, hint: ClassifiedUpload("return-a", "synthetic"))
    dispatch = UploadDispatch(coordinator_db, source)
    dispatch.install()
    assert dispatch.consume(event)
    assert dispatch.begin_extraction(event, source.grant_id(event), EXTRACTION) == "denied"
    assert not dispatch.finish_without_extraction(event)
    with coordinator_db.transaction() as db:
        assert db.execute("SELECT review_required FROM prep_returns").fetchone()["review_required"]
        assert db.execute("SELECT count(*) AS n FROM prep_jobs").fetchone()["n"] == 0


@pytest.mark.parametrize("foreign_original", [False, True])
def test_upload_processor_pins_one_original_and_never_replays_provider(intake, coordinator_db, foreign_original):
    ledger, unavailable = corporate_source(), individual_sources()[0]
    intake.declare(SCOPE, manifest("unauthorized", unavailable))
    upload(intake, "unauthorized", unavailable)
    (denied,) = intake.committed_events()
    intake.declare(SCOPE, manifest("partial", ledger, unavailable, approved=True), policy=EXTRACTION)
    assert upload(intake, "partial", ledger).state == "incomplete"
    committed = next(item for item in intake.committed_events() if item != denied)
    original = OriginalRef(document_id=committed.upload_id, version_id=committed.version_id)
    outside = OriginalRef(document_id=denied.upload_id, version_id=denied.version_id)
    identity = RecordIdentity(
        tax_year=2025, form="business_ledger", subject_reference="TEST-CORP-01", issuer_reference="North Office",
        activity_reference="Other deductions", period="2025", instance_reference="D-101",
    )
    key = "expense-amount"
    anchor = ExactClaim(
        precision="exact", original=original, page=None, line_start=4, line_end=4,
        quote="Ledger entry D-101 | 2025-04-15 | North Office | Office supplies | $125.00",
    )
    value = Amount(kind="amount", value=Decimal("125.00"))
    requests = []

    def generate(request):
        requests.append(request)
        payload = json.loads(json.loads(request.content)["contents"][0]["parts"][0]["text"])
        assert payload["original"] == original.model_dump(by_alias=True)
        assert payload["pages"][0]["lines"][3] == {"line": 4, "text": anchor.quote}
        reported_original = outside if foreign_original else original
        result = {"schemaVersion": 1, "issues": [], "records": [{
            "original": reported_original.model_dump(by_alias=True),
            "identity": identity.model_dump(by_alias=True),
            "candidates": [{
                "factKey": key, "column": "reported", "rawValue": "$125.00",
                "normalized": value.model_dump(mode="json"),
                "citations": [anchor.model_copy(update={"original": reported_original}).model_dump(by_alias=True)],
            }],
        }]}
        return httpx.Response(200, json={
            "candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [{"text": json.dumps(result)}]}}],
            "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 50},
        })

    source = CommittedBatchSource(intake, lambda event, hint: ClassifiedUpload("return-a", "synthetic"))
    dispatch = UploadDispatch(coordinator_db, source)
    dispatch.install()
    with coordinator_db.transaction() as db:
        db.execute("CREATE TABLE test_grounded_artifacts (artifact_id text PRIMARY KEY, grounded jsonb NOT NULL)")

    class PracticeFacts:
        def prepare(self, ticket, extraction):
            media_type, content = source.read_extraction(ticket)
            scope = OriginalScope(firm_id=committed.firm_id, return_id="return-a", original=original)
            loaded = load_original(scope, lambda _: StoredOriginal(scope, media_type, ticket.sha256, content))
            finding = VerifiedFact(scope=scope, identity=identity, fact_key=key, column="reported",
                                   raw_value="$125.00", normalized=value, anchors=(anchor,))
            (grounded,) = ground_occurrence(loaded, extraction.records[0], (finding,))
            assert grounded.eligible and grounded.citations[0].precision == "lines"
            def persist(conn):
                conn.execute("INSERT INTO test_grounded_artifacts VALUES ('grounded-ledger',%s::jsonb)",
                             (grounded.model_dump_json(),))
                return "grounded-ledger"

            return PreparedExtraction(scope.return_id, persist)

    provider = ProviderIdentity("gemini", EXTRACTION.model_id, EXTRACTION.endpoint, EXTRACTION.account_id)
    no_charge = Decimal(0)
    policy = StagePolicy(provider, "extraction", frozenset({"synthetic"}), 1, 100_000, 5,
                         500, 256, no_charge, no_charge, no_charge)

    @asynccontextmanager
    async def open_fixture_provider():
        async with open_provider(provider, api_key="synthetic-key", transport=httpx.MockTransport(generate)) as sdk:
            yield sdk

    async def handle():
        processor = UploadProcessor(dispatch, source, PracticeFacts(), policy, "synthetic", open_fixture_provider)
        assert await processor.handle(UploadCommitted("firm", "not-committed", "version")) == "uncommitted"
        assert await processor.handle(denied) == "denied"
        assert not requests
        assert await processor.handle(committed) == ("needs-attention" if foreign_original else "settled")
        assert await processor.handle(committed) == "no-work"

    asyncio.run(handle())
    assert len(requests) == 1
    with coordinator_db.transaction() as db:
        jobs = db.execute("SELECT snapshot FROM prep_jobs").fetchall()
        assert db.execute("SELECT review_required FROM prep_returns").fetchone()["review_required"]
        artifacts = db.execute("SELECT * FROM test_grounded_artifacts").fetchall()
    if foreign_original:
        assert not jobs and not artifacts
    else:
        (job,) = jobs
        (job_input,) = job["snapshot"]["inputs"]
        (artifact,) = artifacts
        assert job_input["artifact_id"] == artifact["artifact_id"]
        assert artifact["grounded"]["citations"][0]["quote"] == anchor.quote


@pytest.mark.parametrize("changed", ["revoked", "recipient", "class", "assignment", "receipt", "expired"])
def test_provider_transport_rechecks_after_preparing_request(intake, coordinator_db, monkeypatch, changed):
    document = individual_sources()[0]
    intake.declare(SCOPE, manifest("revoked", document, approved=True), policy=EXTRACTION)
    upload(intake, "revoked", document)
    (event,) = intake.committed_events()
    classification = ClassifiedUpload("return-a", "synthetic")
    source = CommittedBatchSource(intake, lambda *_: classification)
    dispatch = UploadDispatch(coordinator_db, source)
    dispatch.install()
    requests = []

    class NoArtifact:
        def prepare(self, *_):
            pytest.fail("Denied requests cannot produce artifacts")

    def generate(request):
        requests.append(request)
        pytest.fail("A changed authorization must never reach the provider")

    provider = ProviderIdentity("gemini", EXTRACTION.model_id, EXTRACTION.endpoint, EXTRACTION.account_id)
    policy = StagePolicy(provider, "extraction", frozenset({"synthetic"}), 1, 100_000, 5,
                         500, 256, Decimal(0), Decimal(0), Decimal(0))

    @asynccontextmanager
    async def open_fixture_provider():
        nonlocal classification
        # The attempt exists and the original has been loaded and numbered, but no request was sent.
        if changed == "class":
            classification = ClassifiedUpload("return-a", "approved_redacted")
        elif changed == "assignment":
            classification = ClassifiedUpload("different-return", "synthetic")
        elif changed == "receipt":
            verify = source.verify
            monkeypatch.setattr(source, "verify", lambda event: replace(verify(event), authorization_scope="different"))
        else:
            update = {
                "revoked": "approved=false",
                "recipient": "policy_endpoint='https://different.example.test'",
                "expired": "consent_expires_at=clock_timestamp()-interval '1 second'",
            }[changed]
            with coordinator_db.transaction() as conn:
                conn.execute("UPDATE prep_upload_batches SET " + update)
        async with open_provider(provider, api_key="synthetic-key", transport=httpx.MockTransport(generate)) as sdk:
            yield sdk

    async def exercise():
        worker = UploadProcessor(dispatch, source, NoArtifact(), policy, "synthetic", open_fixture_provider)
        assert await worker.handle(event) == "denied"
        assert await worker.handle(event) == "no-work"

    asyncio.run(exercise())
    assert not requests
    with coordinator_db.transaction() as conn:
        row = conn.execute("SELECT state,disclosure_started_at,artifact_id FROM prep_dispatch_uploads").fetchone()
        assert row == {
            "state": "misassigned" if changed in {"assignment", "receipt"} else "no_authorization",
            "disclosure_started_at": None, "artifact_id": None,
        }
        assert conn.execute("SELECT count(*) AS n FROM prep_jobs").fetchone()["n"] == 0


@pytest.mark.parametrize("blocked_at", ["parse", "store"])
def test_slow_file_does_not_hold_sibling_commit_or_new_drop(intake, monkeypatch, blocked_at):
    first, second = individual_sources()[:2]
    intake.declare(SCOPE, manifest("same-drop", first, second))
    intake.declare(SCOPE, manifest("other-drop", second))
    started, release = Event(), Event()
    original_store = intake._objects.put_if_absent
    from src.preparation import batches

    original_parse = batches.source_pages

    def blocked(content):
        if content == first.content:
            started.set()
            assert release.wait(10)

    def parse(content, media_type):
        blocked(content)
        return original_parse(content, media_type)

    def store(firm_id, digest, content):
        blocked(content)
        return original_store(firm_id, digest, content)

    if blocked_at == "parse":
        monkeypatch.setattr(batches, "source_pages", parse)
    else:
        monkeypatch.setattr(intake._objects, "put_if_absent", store)

    with ThreadPoolExecutor(max_workers=3) as pool:
        delayed = pool.submit(upload, intake, "same-drop", first)
        try:
            assert started.wait(5)
            sibling = pool.submit(upload, intake, "same-drop", second)
            unrelated = pool.submit(upload, intake, "other-drop", second)
            assert sibling.result(timeout=5).state == "incomplete"
            assert unrelated.result(timeout=5).state == "sealed"
            assert len(intake.committed_events()) == 2
        finally:
            release.set()
        assert delayed.result(timeout=5).state == "sealed"
    assert len(intake.committed_events()) == 3


def test_concurrent_slot_replay_deduplicates_after_storage(intake, monkeypatch):
    (document, *_) = individual_sources()
    intake.declare(SCOPE, manifest("concurrent", document))
    concurrent = Barrier(2)
    store = intake._objects.put_if_absent

    def synchronize(*args):
        concurrent.wait(timeout=5)
        return store(*args)

    monkeypatch.setattr(intake._objects, "put_if_absent", synchronize)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(upload, intake, "concurrent", document)
        second = pool.submit(upload, intake, "concurrent", document)
        assert first.result(timeout=10) == second.result(timeout=10)
    (event,) = intake.committed_events()
    (slot,) = intake.status(SCOPE, "concurrent").files
    assert (slot.upload_id, slot.version_id) == (event.upload_id, event.version_id)


def test_return_intake_scope_uses_attribution_not_drop_hint(intake, coordinator_db):
    first, second, pending = individual_sources()[:3]
    scope = replace(SCOPE, return_ids=frozenset({"return-a", "return-b"}))
    event_a = None
    source = CommittedBatchSource(intake, lambda event, _: ClassifiedUpload(
        "return-a" if event.upload_id == event_a else "return-b", "synthetic",
    ))
    dispatch = UploadDispatch(coordinator_db, source)
    dispatch.install()
    with coordinator_db.transaction() as conn:
        conn.execute("""INSERT INTO prep_returns (firm_id,return_id,profile_id,store_id,tax_year)
                        VALUES ('firm','return-b','practice','store',2025)""")
    intake.declare(scope, manifest("mixed", first, second, pending))
    assert intake.list_batches(scope, return_id="return-a") == ()
    with pytest.raises(ReviewError) as bad_upload:
        intake.put(scope, "mixed", first.document_id, first.media_type, first.content, return_id="return-a")
    assert bad_upload.value.status_code == 404
    with pytest.raises(ReviewError) as bad_exclusion:
        intake.exclude(scope, "mixed", pending.document_id, return_id="return-a")
    assert bad_exclusion.value.status_code == 404
    uploaded_a = intake.put(scope, "mixed", first.document_id, first.media_type, first.content)
    event_a = next(file.upload_id for file in uploaded_a.files if file.slot == first.document_id)
    intake.put(scope, "mixed", second.document_id, second.media_type, second.content)
    assert intake.list_batches(scope, return_id="return-a") == ()
    for event in intake.committed_events():
        assert dispatch.consume(event)
    (visible_a,) = intake.list_batches(scope, return_id="return-a")
    (visible_b,) = intake.list_batches(scope, return_id="return-b")
    assert [slot.slot for slot in visible_a.files] == [first.document_id]
    assert [slot.slot for slot in visible_b.files] == [second.document_id]
    assert all(view.state == "sealed" for view in (visible_a, visible_b))
    assert intake.put(scope, "mixed", first.document_id, first.media_type, first.content,
                      return_id="return-a") == visible_a
    with pytest.raises(ReviewError) as other_return:
        intake.put(scope, "mixed", second.document_id, second.media_type, second.content, return_id="return-a")
    assert other_return.value.status_code == 404
    with pytest.raises(ReviewError) as inaccessible:
        intake.list_batches(SCOPE, return_id="return-b")
    assert inaccessible.value.status_code == 404
    (full_drop,) = intake.list_batches(scope)
    assert len(full_drop.files) == 3 and full_drop.state == "incomplete"
